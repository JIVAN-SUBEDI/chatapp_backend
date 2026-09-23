import json
import mimetypes
from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import render
from django.utils import timezone
from livekit import api
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .firebase import send_incoming_call_push, send_message_push
from .models import (
    CallParticipant,
    CallSession,
    Conversation,
    ConversationMember,
    Message,
    MessageAttachment,
    UserFCMToken,
)
from .serializers import ConversationSerializer, MessageSerializer

User = get_user_model()


def detect_message_type(file):
    if not file:
        return Message.TEXT

    # UploadedFile.content_type is normally more reliable than the filename.
    mime_type = getattr(file, "content_type", None)

    if not mime_type:
        mime_type, _ = mimetypes.guess_type(file.name)

    if not mime_type:
        return Message.FILE

    if mime_type.startswith("image/"):
        return Message.IMAGE
    if mime_type.startswith("video/"):
        return Message.VIDEO
    if mime_type.startswith("audio/"):
        return Message.AUDIO

    return Message.FILE


def _get_uploaded_media(request):
    """Return every uploaded media file while supporting common field names."""
    files = request.FILES.getlist("media")

    # Some frontends submit arrays using media[].
    if not files:
        files = request.FILES.getlist("media[]")

    return files


def _group_message_type(files, requested_type=None):
    valid_types = {value for value, _ in Message.MESSAGE_TYPES}

    if requested_type in valid_types:
        return requested_type

    if not files:
        return Message.TEXT

    detected_types = [detect_message_type(file) for file in files]

    # If every attachment is the same kind, expose that kind on Message.
    if len(set(detected_types)) == 1:
        return detected_types[0]

    # Mixed image/video/file batches are represented as a general file message.
    return Message.FILE


class MyConversationsView(generics.ListAPIView):
    serializer_class = ConversationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Conversation.objects.filter(
            members__user=self.request.user
        ).distinct().order_by("-updated_at")


class StartPrivateChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        other_user_id = request.data.get("user_id")

        if not other_user_id:
            return Response({"error": "user_id is required"}, status=400)

        try:
            other_user_id = int(other_user_id)
        except ValueError:
            return Response({"error": "Invalid user_id"}, status=400)

        if other_user_id == request.user.id:
            return Response({"error": "You cannot chat with yourself"}, status=400)

        try:
            other_user = User.objects.get(id=other_user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)

        existing = (
            Conversation.objects
            .filter(type=Conversation.PRIVATE)
            .filter(members__user=request.user)
            .filter(members__user=other_user)
            .distinct()
            .first()
        )

        if existing:
            return Response(
                ConversationSerializer(existing).data,
                status=status.HTTP_200_OK
            )

        conversation = Conversation.objects.create(
            type=Conversation.PRIVATE,
            created_by=request.user
        )

        ConversationMember.objects.create(
            conversation=conversation,
            user=request.user
        )

        ConversationMember.objects.create(
            conversation=conversation,
            user=other_user
        )

        return Response(
            ConversationSerializer(conversation).data,
            status=status.HTTP_201_CREATED
        )  

         

class CreateGroupChatView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        name = str(request.data.get("name", "")).strip()

        if not name:
            return Response(
                {"error": "Group name is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ---------------------------------------------------------
        # READ MEMBER IDS
        # Dio multipart may send member_ids in different forms.
        # Support:
        # member_ids=1&member_ids=2
        # member_ids[]=1&member_ids[]=2
        # member_ids=["1","2"]
        # ---------------------------------------------------------

        member_ids = request.data.getlist("member_ids")

        if not member_ids:
            member_ids = request.data.getlist("member_ids[]")

        # If DRF received a normal list
        if not member_ids:
            raw_member_ids = request.data.get("member_ids", [])

            if isinstance(raw_member_ids, list):
                member_ids = raw_member_ids

            elif raw_member_ids:
                # Could be JSON string
                try:
                    decoded = json.loads(raw_member_ids)

                    if isinstance(decoded, list):
                        member_ids = decoded
                    else:
                        member_ids = [raw_member_ids]

                except (json.JSONDecodeError, TypeError):
                    member_ids = [raw_member_ids]

        # ---------------------------------------------------------
        # CLEAN IDS
        # ---------------------------------------------------------

        cleaned_member_ids = []

        for value in member_ids:
            try:
                user_id = int(value)

                if user_id != request.user.id:
                    cleaned_member_ids.append(user_id)

            except (TypeError, ValueError):
                continue

        cleaned_member_ids = list(set(cleaned_member_ids))

        # ---------------------------------------------------------
        # IMPORTANT:
        # Flutter sends "group_image", not "image".
        # ---------------------------------------------------------

        group_image = (
            request.FILES.get("group_image")
            or request.FILES.get("image")
        )

        try:
            with transaction.atomic():

                conversation = Conversation.objects.create(
                    type=Conversation.GROUP,
                    name=name,
                    image=group_image,
                    created_by=request.user,
                )

                # Creator = member + admin
                ConversationMember.objects.create(
                    conversation=conversation,
                    user=request.user,
                    is_admin=True,
                )

                users = User.objects.filter(
                    id__in=cleaned_member_ids
                )

                for user in users:
                    ConversationMember.objects.get_or_create(
                        conversation=conversation,
                        user=user,
                        defaults={
                            "is_admin": False,
                        },
                    )

        except Exception as exc:
            return Response(
                {
                    "error": "Unable to create group",
                    "details": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ---------------------------------------------------------
        # SERIALIZE COMPLETE GROUP
        # ---------------------------------------------------------

        data = ConversationSerializer(
            conversation,
            context={"request": request},
        ).data
        # ============================================================
        # REALTIME: NOTIFY EVERY GROUP MEMBER
        # ============================================================

        channel_layer = get_channel_layer()

        actual_member_ids = list(
            ConversationMember.objects.filter(
                conversation=conversation,
                is_blocked=False,
            ).values_list(
                "user_id",
                flat=True,
            )
        )

        for user_id in actual_member_ids:
            async_to_sync(
                channel_layer.group_send
            )(
                f"user_chat_{user_id}",
                {
                    "type": "conversation_created",
                    "data": data,
                },
            )
        print("")
        print("======================================")
        print("GROUP CREATED")
        print("GROUP ID:", conversation.id)
        print("GROUP NAME:", conversation.name)
        print("CREATOR:", request.user.id)
        print("REQUEST MEMBER IDS:", member_ids)
        print("CLEAN MEMBER IDS:", cleaned_member_ids)
        print(
            "ACTUAL MEMBERS:",
            list(
                ConversationMember.objects.filter(
                    conversation=conversation
                ).values_list(
                    "user_id",
                    flat=True,
                )
            ),
        )
        print("SERIALIZED GROUP:", data)
        print("======================================")
        print("")

        return Response(
            data,
            status=status.HTTP_201_CREATED,
        )

class UpdateGroupInfoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, conversation_id):
        conversation = Conversation.objects.filter(
            id=conversation_id,
            type=Conversation.GROUP
        ).first()

        if not conversation:
            return Response(
                {"error": "Group not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        is_admin = ConversationMember.objects.filter(
            conversation=conversation,
            user=request.user,
            is_admin=True
        ).exists()

        if not is_admin:
            return Response(
                {"error": "Only group admin can update group info"},
                status=status.HTTP_403_FORBIDDEN
            )

        name = request.data.get("name")
        image = request.FILES.get("image")

        if name is not None:
            name = name.strip()
            if not name:
                return Response(
                    {"error": "Group name cannot be empty"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            conversation.name = name

        if image is not None:
            conversation.image = image

        conversation.save()

        data = ConversationSerializer(
            conversation,
            context={"request": request}
        ).data

        channel_layer = get_channel_layer()

        async_to_sync(channel_layer.group_send)(
            f"chat_{conversation_id}",
            {
                "type": "group_info_updated",
                "conversation": data,
            }
        )

        return Response(
            {
                "success": True,
                "conversation": data,
            },
            status=status.HTTP_200_OK
        )

class AddGroupMemberView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id):
        user_id = request.data.get("user_id")

        is_admin = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user=request.user,
            is_admin=True
        ).exists()

        if not is_admin:
            return Response({"error": "Only admin can add members"}, status=403)

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)

        member, created = ConversationMember.objects.get_or_create(
            conversation_id=conversation_id,
            user=user
        )

        return Response({"success": True, "created": created})


class RemoveGroupMemberView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, conversation_id, user_id):
        is_admin = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user=request.user,
            is_admin=True
        ).exists()

        if not is_admin:
            return Response({"error": "Only admin can remove members"}, status=403)

        ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id
        ).delete()

        return Response({"success": True})


class ConversationMessagesView(generics.ListAPIView):
    serializer_class = MessageSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        conversation_id = self.kwargs["conversation_id"]

        is_member = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user=self.request.user,
        ).exists()

        if not is_member:
            return Message.objects.none()

        return (
            Message.objects
            .filter(conversation_id=conversation_id)
            .select_related("sender", "reply_to__sender", "reaction_to__sender")
            .prefetch_related("attachments", "read_by")
        )


class SendMessageView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id):
        member = (
            ConversationMember.objects
            .select_related("conversation")
            .filter(
                conversation_id=conversation_id,
                user=request.user,
            )
            .first()
        )

        if not member:
            return Response(
                {"error": "You are not a member"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if member.is_blocked:
            return Response(
                {"error": "You are blocked in this chat"},
                status=status.HTTP_403_FORBIDDEN,
            )

        conversation = member.conversation
        uploaded_files = _get_uploaded_media(request)
        uploaded_thumbnails = request.FILES.getlist("thumbnail")

        if not uploaded_thumbnails:
            uploaded_thumbnails = request.FILES.getlist("thumbnail[]")

        text = str(request.data.get("text", "")).strip()
        requested_type = request.data.get("message_type")

        if not text and not uploaded_files:
            return Response(
                {"error": "Text or at least one media file is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message_type = _group_message_type(
            uploaded_files,
            requested_type=requested_type,
        )

        # Keep the original Message.media behavior for exactly one file.
        # Multiple files are stored as ordered MessageAttachment rows.
        single_file = uploaded_files[0] if len(uploaded_files) == 1 else None
        single_thumbnail = (
            uploaded_thumbnails[0]
            if single_file and uploaded_thumbnails
            else None
        )

        try:
            with transaction.atomic():
                message = Message.objects.create(
                    conversation=conversation,
                    sender=request.user,
                    message_type=message_type,
                    text=text,
                    media=single_file,
                    thumbnail=single_thumbnail,
                    file_name=single_file.name if single_file else None,
                    file_size=single_file.size if single_file else None,
                    mime_type=(
                        getattr(single_file, "content_type", None)
                        if single_file
                        else None
                    ),
                    duration=(
                        request.data.get("duration") or None
                        if single_file
                        else None
                    ),
                    reply_to_id=request.data.get("reply_to") or None,
                    reaction_to_id=request.data.get("reaction_to") or None,
                    reaction=request.data.get("reaction") or None,
                )

                if len(uploaded_files) > 1:
                    for index, uploaded_file in enumerate(uploaded_files):
                        thumbnail = (
                            uploaded_thumbnails[index]
                            if index < len(uploaded_thumbnails)
                            else None
                        )

                        # Use create() instead of bulk_create() so FileField
                        # storage handling runs normally for every upload.
                        MessageAttachment.objects.create(
                            message=message,
                            file=uploaded_file,
                            thumbnail=thumbnail,
                            attachment_type=detect_message_type(
                                uploaded_file
                            ),
                            file_name=uploaded_file.name,
                            file_size=uploaded_file.size,
                            mime_type=getattr(
                                uploaded_file,
                                "content_type",
                                None,
                            ),
                            order=index,
                        )

                message.read_by.add(request.user)

                # Trigger Conversation.updated_at without modifying other fields.
                conversation.save(update_fields=["updated_at"])

        except (IntegrityError, ValueError) as exc:
            return Response(
                {
                    "error": "Unable to send message",
                    "details": str(exc),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Reload relations so the response and WebSocket include attachments.
        message = (
            Message.objects
            .select_related("sender", "reply_to__sender", "reaction_to__sender")
            .prefetch_related("attachments", "read_by")
            .get(pk=message.pk)
        )

        data = MessageSerializer(
            message,
            context={"request": request},
        ).data

        channel_layer = get_channel_layer()

        async_to_sync(channel_layer.group_send)(
            f"chat_{conversation_id}",
            {
                "type": "chat_message",
                "message": data,
            },
        )

        sender_name = (
            getattr(request.user, "full_name", None)
            or getattr(request.user, "name", None)
            or getattr(request.user, "username", "")
            or str(request.user.id)
        )

        sender_avatar = ""
        profile_picture = getattr(request.user, "profile_picture", None)

        if profile_picture:
            try:
                sender_avatar = request.build_absolute_uri(
                    profile_picture.url
                )
            except (AttributeError, ValueError):
                sender_avatar = ""

        media_count = len(uploaded_files)

        if media_count > 1:
            detected_types = [
                detect_message_type(file) for file in uploaded_files
            ]

            if all(kind == Message.IMAGE for kind in detected_types):
                body = f"Sent {media_count} images"
            elif all(kind == Message.VIDEO for kind in detected_types):
                body = f"Sent {media_count} videos"
            else:
                body = f"Sent {media_count} attachments"
        elif message.message_type == Message.TEXT:
            body = message.text or "New message"
        elif message.message_type == Message.IMAGE:
            body = "Sent an image"
        elif message.message_type == Message.VIDEO:
            body = "Sent a video"
        elif message.message_type == Message.AUDIO:
            body = "Sent an audio"
        else:
            body = "Sent a file"

        if conversation.type == Conversation.GROUP:
            title = conversation.name or sender_name
            body = f"{sender_name}: {body}"
        else:
            title = sender_name

        message_data = {
            "title": title,
            "body": body,
            "conversation_id": conversation.id,
            "message_id": message.id,
            "sender_id": request.user.id,
            "sender_name": sender_name,
            "sender_avatar": sender_avatar,
            "message_type": message.message_type,
            "text": message.text or "",
            "attachment_count": media_count,
            "has_multiple_attachments": media_count > 1,
        }

        receiver_ids = (
            ConversationMember.objects
            .filter(
                conversation=conversation,
                is_blocked=False,
            )
            .exclude(user=request.user)
            .values_list("user_id", flat=True)
        )

        tokens = UserFCMToken.objects.filter(
            user_id__in=receiver_ids,
            is_active=True,
        )

        for item in tokens.iterator():
            try:
                send_message_push(
                    token=item.token,
                    message_data=message_data,
                )
            except Exception:
                item.is_active = False
                item.save(update_fields=["is_active"])

        return Response(
            data,
            status=status.HTTP_201_CREATED,
        )


class EditMessageView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, message_id):
        try:
            message = Message.objects.get(
                id=message_id,
                sender=request.user,
            )
        except Message.DoesNotExist:
            return Response(
                {"error": "Message not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if message.is_deleted:
            return Response(
                {"error": "Message is deleted"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message.text = request.data.get("text", message.text)
        message.is_edited = True
        message.save(update_fields=["text", "is_edited", "updated_at"])

        return Response(
            MessageSerializer(
                message,
                context={"request": request},
            ).data
        )


class DeleteMessageView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, message_id):
        try:
            message = Message.objects.prefetch_related(
                "attachments"
            ).get(
                id=message_id,
                sender=request.user,
            )
        except Message.DoesNotExist:
            return Response(
                {"error": "Message not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if message.is_deleted:
            return Response({"success": True})

        # Remove old single-file media from storage.
        if message.media:
            message.media.delete(save=False)

        if message.thumbnail:
            message.thumbnail.delete(save=False)

        # Remove all files belonging to grouped attachments.
        for attachment in message.attachments.all():
            if attachment.file:
                attachment.file.delete(save=False)

            if attachment.thumbnail:
                attachment.thumbnail.delete(save=False)

        message.attachments.all().delete()

        message.text = ""
        message.media = None
        message.thumbnail = None
        message.file_name = None
        message.file_size = None
        message.mime_type = None
        message.duration = None
        message.is_deleted = True
        message.save(
            update_fields=[
                "text",
                "media",
                "thumbnail",
                "file_name",
                "file_size",
                "mime_type",
                "duration",
                "is_deleted",
                "updated_at",
            ]
        )

        return Response({"success": True})


class MarkMessageReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, message_id):
        try:
            message = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return Response({"error": "Message not found"}, status=404)

        is_member = ConversationMember.objects.filter(
            conversation=message.conversation,
            user=request.user
        ).exists()

        if not is_member:
            return Response({"error": "You are not a member"}, status=403)

        message.read_by.add(request.user)

        return Response({"success": True})


class MarkConversationReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id):
        is_member = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user=request.user
        ).exists()

        if not is_member:
            return Response({"error": "You are not a member"}, status=403)

        messages = Message.objects.filter(conversation_id=conversation_id)

        for message in messages:
            message.read_by.add(request.user)

        return Response({"success": True})
def chat_test_page(request):
    return render(request, "chats.html")


class SearchUserByPhoneView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        phone = request.GET.get("phone")

        if not phone:
            return Response(
                {"error": "phone is required"},
                status=400
            )

        try:
            user = User.objects.get(phone=phone)
            image = getattr(user, "profile_picture", None)
            image_url = ""
            if image:
                image_url = request.build_absolute_uri(image.url)
            return Response({
                "id": user.id,
                "name": getattr(user, "full_name", ""),
                "profile_picture":image_url,
                "phone_number": getattr(user, "phone", ""),
            })

        except User.DoesNotExist:
            return Response(
                {"error": "User not found"},
                status=404
            )
class UpdateMemberNicknameView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, conversation_id, user_id):
        if not ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user=request.user
        ).exists():
            return Response({"error": "You are not a member"}, status=403)

        member = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id
        ).first()

        if not member:
            return Response({"error": "Member not found"}, status=404)

        member.nickname = request.data.get("nickname", "")
        member.save()

        return Response({"success": True, "nickname": member.nickname})
class BlockGroupMemberView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id, user_id):
        is_admin = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user=request.user,
            is_admin=True
        ).exists()

        if not is_admin:
            return Response(
                {"error": "Only admin can block members"},
                status=403
            )

        # Admin should not block himself
        if int(user_id) == request.user.id:
            return Response(
                {"error": "You cannot block yourself"},
                status=400
            )

        member = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id
        ).first()

        if not member:
            return Response(
                {"error": "Member not found"},
                status=404
            )

        member.is_blocked = True
        member.blocked_by = request.user
        member.save()

        return Response({
            "success": True,
            "message": "Member blocked successfully"
        })


class UnblockGroupMemberView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id, user_id):
        is_admin = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user=request.user,
            is_admin=True
        ).exists()

        if not is_admin:
            return Response(
                {"error": "Only admin can unblock members"},
                status=403
            )

        member = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id
        ).first()

        if not member:
            return Response(
                {"error": "Member not found"},
                status=404
            )

        member.is_blocked = False
        member.blocked_by = None
        member.save()

        return Response({
            "success": True,
            "message": "Member unblocked successfully"
        })
class BlockPrivateUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id, user_id):
        conversation = Conversation.objects.filter(
            id=conversation_id,
            type=Conversation.PRIVATE
        ).first()

        if not conversation:
            return Response(
                {"error": "Private conversation not found"},
                status=404
            )

        # Request user must be member of this private chat
        requester_member = ConversationMember.objects.filter(
            conversation=conversation,
            user=request.user
        ).first()

        if not requester_member:
            return Response(
                {"error": "You are not a member of this chat"},
                status=403
            )

        # Target user must also be member of this private chat
        target_member = ConversationMember.objects.filter(
            conversation=conversation,
            user_id=user_id
        ).first()

        if not target_member:
            return Response(
                {"error": "User not found in this chat"},
                status=404
            )

        if int(user_id) == request.user.id:
            return Response(
                {"error": "You cannot block yourself"},
                status=400
            )

        target_member.is_blocked = True
        target_member.blocked_by = request.user
        target_member.save()

        return Response({
            "success": True,
            "message": "User blocked successfully"
        })


class UnblockPrivateUserView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id, user_id):
        conversation = Conversation.objects.filter(
            id=conversation_id,
            type=Conversation.PRIVATE
        ).first()

        if not conversation:
            return Response(
                {"error": "Private conversation not found"},
                status=404
            )

        requester_member = ConversationMember.objects.filter(
            conversation=conversation,
            user=request.user
        ).first()

        if not requester_member:
            return Response(
                {"error": "You are not a member of this chat"},
                status=403
            )

        target_member = ConversationMember.objects.filter(
            conversation=conversation,
            user_id=user_id
        ).first()

        if not target_member:
            return Response(
                {"error": "User not found in this chat"},
                status=404
            )

        if target_member.blocked_by != request.user:
            return Response(
                {"error": "You can only unblock users blocked by you"},
                status=403
            )

        target_member.is_blocked = False
        target_member.blocked_by = None
        target_member.save()

        return Response({
            "success": True,
            "message": "User unblocked successfully"
        })
    
class SaveFCMTokenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        token = request.data.get("token")
        device_id = request.data.get("device_id")
        platform = request.data.get("platform")

        if not token:
            return Response(
                {"error": "token is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        UserFCMToken.objects.update_or_create(
            token=token,
            defaults={
                "user": request.user,
                "device_id": device_id,
                "platform": platform,
                "is_active": True,
            },
        )

        return Response({"success": True})


def _as_boolean(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _user_display_name(user):
    return (
        getattr(user, "full_name", None)
        or getattr(user, "name", None)
        or getattr(user, "username", None)
        or getattr(user, "phone", None)
        or str(user.id)
    )


def _user_avatar_url(request, user):
    image = (
        getattr(user, "profile_picture", None)
        or getattr(user, "avatar", None)
    )

    if not image:
        return ""

    try:
        return request.build_absolute_uri(image.url)
    except (AttributeError, ValueError):
        return str(image) if image else ""


def _get_call_session(call_id, lock=False):
    queryset = CallSession.objects.select_related(
        "conversation",
        "caller",
        "receiver",
    )

    if lock:
        queryset = queryset.select_for_update()

    value = str(call_id).strip()

    if value.isdigit():
        return queryset.filter(pk=int(value)).first()

    try:
        return queryset.filter(call_uuid=value).first()
    except (ValidationError, ValueError):
        return None


def _broadcast_call_event(conversation_id, data):
    channel_layer = get_channel_layer()

    async_to_sync(channel_layer.group_send)(
        f"chat_{conversation_id}",
        {
            "type": "call_event",
            "data": data,
        },
    )


def _serialize_call(call):
    return {
        "call_id": call.id,
        "call_uuid": str(call.call_uuid),
        "conversation_id": call.conversation_id,
        "conversation_type": call.conversation.type,
        "is_group_call": call.conversation.type == Conversation.GROUP,
        "caller_id": call.caller_id,
        "receiver_id": call.receiver_id,
        "call_type": call.call_type,
        "is_video_call": call.call_type == CallSession.VIDEO,
        "status": call.status,
        "created_at": call.created_at.isoformat(),
        "answered_at": (
            call.answered_at.isoformat()
            if call.answered_at
            else None
        ),
        "ended_at": (
            call.ended_at.isoformat()
            if call.ended_at
            else None
        ),
    }
# ============================================================
# CALL TIMEOUT
# ============================================================

CALL_RING_TIMEOUT = timedelta(seconds=45)


def _expire_stale_ringing_calls(conversation=None):


    cutoff = timezone.now() - CALL_RING_TIMEOUT
    now = timezone.now()

    expired_calls = []

    with transaction.atomic():

        queryset = (
            CallSession.objects
            .select_for_update()
            .select_related("conversation")
            .filter(
                status=CallSession.RINGING,
                created_at__lt=cutoff,
            )
        )

        if conversation is not None:
            queryset = queryset.filter(conversation=conversation)

        calls = list(queryset)

        for call in calls:

            # The whole call is now missed.
            call.status = CallSession.MISSED
            call.ended_at = now

            call.save(
                update_fields=[
                    "status",
                    "ended_at",
                ]
            )

            # People who were still ringing missed the call.
            CallParticipant.objects.filter(
                call=call,
                status=CallParticipant.RINGING,
            ).update(
                status=CallParticipant.MISSED,
                left_at=now,
            )

            # Caller / already joined participant is no longer active.
            CallParticipant.objects.filter(
                call=call,
                status=CallParticipant.JOINED,
            ).update(
                status=CallParticipant.LEFT,
                left_at=now,
            )

            expired_calls.append(call)

    # Broadcast after DB transaction
    for call in expired_calls:
        _broadcast_call_event(
            call.conversation_id,
            {
                "type": "call_status_updated",
                **_serialize_call(call),
                "action": "timeout",
            },
        )

    return len(expired_calls)

class StartCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        conversation_id = request.data.get("conversation_id")
        receiver_id = request.data.get("receiver_id")
        is_video_call = _as_boolean(
            request.data.get("is_video_call", False)
        )

        if not conversation_id:
            return Response(
                {"error": "conversation_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        conversation = Conversation.objects.filter(
            id=conversation_id
        ).first()

        if not conversation:
            return Response(
                {"error": "Conversation not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        caller_membership = ConversationMember.objects.filter(
            conversation=conversation,
            user=request.user,
            is_blocked=False,
        ).first()

        if not caller_membership:
            return Response(
                {
                    "error": (
                        "You are not an active member of this conversation"
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        active_members = list(
            ConversationMember.objects.filter(
                conversation=conversation,
                is_blocked=False,
            )
            .select_related("user")
            .order_by("id")
        )

        if len(active_members) < 2:
            return Response(
                {"error": "At least two active members are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        receiver = None

        if conversation.type == Conversation.PRIVATE:
            other_members = [
                member
                for member in active_members
                if member.user_id != request.user.id
            ]

            if len(other_members) != 1:
                return Response(
                    {
                        "error": (
                            "Private conversation must contain exactly "
                            "two active members"
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            receiver = other_members[0].user

            if receiver_id is not None:
                try:
                    supplied_receiver_id = int(receiver_id)
                except (TypeError, ValueError):
                    return Response(
                        {"error": "Invalid receiver_id"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                if supplied_receiver_id != receiver.id:
                    return Response(
                        {
                            "error": (
                                "receiver_id is not the other member "
                                "of this private conversation"
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

        elif conversation.type != Conversation.GROUP:
            return Response(
                {"error": "Unsupported conversation type"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ------------------------------------------------------------
        # CLEAN DEAD / STALE RINGING CALLS
        # ------------------------------------------------------------

        _expire_stale_ringing_calls(
            conversation=conversation
        )

        existing_call = CallSession.objects.filter(
            conversation=conversation,
            status__in=[
                CallSession.RINGING,
                CallSession.ACCEPTED,
            ],
        ).first()

        if existing_call:
            return Response(
                {
                    "error": "A call is already active in this conversation",
                    "call_id": existing_call.id,
                    "call_uuid": str(existing_call.call_uuid),
                    "status": existing_call.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        now = timezone.now()

        try:
            with transaction.atomic():
                call = CallSession.objects.create(
                    conversation=conversation,
                    caller=request.user,
                    receiver=receiver,
                    call_type=(
                        CallSession.VIDEO
                        if is_video_call
                        else CallSession.AUDIO
                    ),
                )

                participants = []

                for member in active_members:
                    is_caller = member.user_id == request.user.id

                    participants.append(
                        CallParticipant(
                            call=call,
                            user=member.user,
                            status=(
                                CallParticipant.JOINED
                                if is_caller
                                else CallParticipant.RINGING
                            ),
                            joined_at=now if is_caller else None,
                            is_camera_enabled=(
                                is_video_call and is_caller
                            ),
                        )
                    )

                CallParticipant.objects.bulk_create(participants)

        except (IntegrityError, ValidationError) as exc:
            return Response(
                {
                    "error": (
                        "Unable to start call. Another active call may "
                        "already exist in this conversation."
                    ),
                    "details": str(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )

        caller_name = _user_display_name(request.user)
        caller_avatar = _user_avatar_url(request, request.user)

        call_data = {
            "type": "incoming_call",
            **_serialize_call(call),
            "caller_name": caller_name,
            "caller_avatar": caller_avatar,
            "conversation_name": conversation.name or "",
        }

        invited_user_ids = [
            member.user_id
            for member in active_members
            if member.user_id != request.user.id
        ]

        tokens = UserFCMToken.objects.filter(
            user_id__in=invited_user_ids,
            # is_active=True,
        )

        sent = 0
        # print("items:"+item)
        print("it is a item")
        print("====================================")
        print("INVITED USER IDS:", invited_user_ids)

        print(
            "ALL FCM TOKENS:",
            list(
                UserFCMToken.objects.all().values(
                    "id",
                    "user_id",
                    "token",
                    "is_active",
                )
            )
        )

        print(
            "TOKENS FOR INVITED USERS:",
            list(
                UserFCMToken.objects.filter(
                    user_id__in=invited_user_ids
                ).values(
                    "id",
                    "user_id",
                    "token",
                    "is_active",
                )
            )
        )

        print(
            "ACTIVE TOKENS:",
            list(
                UserFCMToken.objects.filter(
                    user_id__in=invited_user_ids,
                    is_active=True,
                ).values(
                    "id",
                    "user_id",
                    "token",
                    "is_active",
                )
            )
        )

        print("====================================")
        for item in tokens.iterator():
            print("actually called things")
            try:
                send_incoming_call_push(
                    token=item.token,
                    call_data=call_data,
                )
                sent += 1
            except Exception:
                item.is_active = False
                item.save(update_fields=["is_active"])

        _broadcast_call_event(conversation.id, call_data)

        return Response(
            {
                "success": True,
                **_serialize_call(call),
                "livekit_room_name": call.livekit_room_name,
                "invited_user_ids": invited_user_ids,
                "push_sent": sent,
            },
            status=status.HTTP_201_CREATED,
        )


class LiveKitTokenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        call_id = request.data.get("call_id")

        if not call_id:
            return Response(
                {"error": "call_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        call = _get_call_session(call_id)

        if not call:
            return Response(
                {"error": "Call not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if call.status in {
            CallSession.ENDED,
            CallSession.REJECTED,
            CallSession.MISSED,
            CallSession.CANCELLED,
        }:
            return Response(
                {"error": "This call is no longer active"},
                status=status.HTTP_409_CONFLICT,
            )

        membership = ConversationMember.objects.filter(
            conversation=call.conversation,
            user=request.user,
            is_blocked=False,
        ).first()

        if not membership:
            return Response(
                {"error": "You are not allowed to join this call"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # is_group_call = call.conversation.type == Conversation.GROUP

        participant = CallParticipant.objects.filter(
            call=call,
            user=request.user,
        ).first()


        # ------------------------------------------------------------
        # GROUP CALL:
        # Any CURRENT active group member may join an ongoing call.
        #
        # This also supports:
        # - user left and joins again
        # - user declined and joins later
        # - user missed and joins later
        # - user was added to group after call started
        # ------------------------------------------------------------
        if not participant :
            participant = CallParticipant.objects.create(
                call=call,
                user=request.user,
                status=CallParticipant.INVITED,
            )


        # Private calls must already contain the participant.
        if not participant:
            return Response(
                {"error": "You are not allowed to join this call"},
                status=status.HTTP_403_FORBIDDEN,
            )


        # For PRIVATE calls preserve existing decline/missed behaviour.
        if (participant.status in {
                CallParticipant.DECLINED,
                CallParticipant.MISSED,
            }
        ):
            return Response(
                {"error": "You already declined or missed this call"},
                status=status.HTTP_409_CONFLICT,
            )

        livekit_url = getattr(settings, "LIVEKIT_URL", "")
        livekit_api_key = getattr(settings, "LIVEKIT_API_KEY", "")
        livekit_api_secret = getattr(
            settings,
            "LIVEKIT_API_SECRET",
            "",
        )

        if not all(
            [
                livekit_url,
                livekit_api_key,
                livekit_api_secret,
            ]
        ):
            return Response(
                {"error": "LiveKit settings are incomplete"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if not call.livekit_room_name:
            call.livekit_room_name = f"hiddenly-call-{call.call_uuid.hex}"
            call.save(update_fields=["livekit_room_name"])

        participant_name = _user_display_name(request.user)
        participant_avatar = _user_avatar_url(request, request.user)
        participant_identity = f"user-{request.user.id}"

        participant_metadata = json.dumps(
            {
                "user_id": request.user.id,
                "full_name": participant_name,
                "profile_picture": participant_avatar,
                "conversation_id": call.conversation_id,
                "call_id": call.id,
                "call_uuid": str(call.call_uuid),
            }
        )

        try:
            participant_token = (
                api.AccessToken(
                    livekit_api_key,
                    livekit_api_secret,
                )
                .with_identity(participant_identity)
                .with_name(participant_name)
                .with_metadata(participant_metadata)
                .with_ttl(timedelta(hours=2))
                .with_grants(
                    api.VideoGrants(
                        room_join=True,
                        room=call.livekit_room_name,
                        can_publish=True,
                        can_subscribe=True,
                        can_publish_data=True,
                    )
                )
                .to_jwt()
            )
        except (TypeError, ValueError) as exc:
            return Response(
                {
                    "error": "Unable to generate LiveKit token",
                    "details": str(exc),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        joined_at = timezone.now()

        update_fields = ["status"]
        participant.status = CallParticipant.JOINED

        joined_at = timezone.now()

        participant.status = CallParticipant.JOINED
        participant.joined_at = joined_at
        participant.left_at = None

        participant.save(
            update_fields=[
                "status",
                "joined_at",
                "left_at",
            ]
        )


        # The caller joining should not count as the call being answered.
        if (
            request.user.id != call.caller_id
            and call.status == CallSession.RINGING
        ):
            call.status = CallSession.ACCEPTED
            call.answered_at = joined_at
            call.save(update_fields=["status", "answered_at"])

        event_data = {
            "type": "call_participant_joined",
            **_serialize_call(call),
            "participant": {
                "user_id": request.user.id,
                "name": participant_name,
                "profile_picture": participant_avatar,
                "status": participant.status,
                "joined_at": participant.joined_at.isoformat(),
            },
        }

        _broadcast_call_event(call.conversation_id, event_data)

        return Response(
            {
                "server_url": livekit_url,
                "participant_token": participant_token,
                "room_name": call.livekit_room_name,
                "call_id": call.id,
                "call_uuid": str(call.call_uuid),
                "call_type": call.call_type,
                "is_video_call": call.call_type == CallSession.VIDEO,
                "participant_identity": participant_identity,
            },
            status=status.HTTP_201_CREATED,
        )


class UpdateCallStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        action = str(request.data.get("action", "")).strip().lower()

        valid_actions = {
            "accept",
            "reject",
            "ended",
            "leave",
            "missed",
            "cancel",
        }

        if action not in valid_actions:
            return Response(
                {"error": "Invalid action"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            call = _get_call_session(call_id, lock=True)

            if not call:
                return Response(
                    {"error": "Call not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            participant = CallParticipant.objects.select_for_update().filter(
                call=call,
                user=request.user,
            ).first()

            if not participant:
                return Response(
                    {"error": "Not allowed"},
                    status=status.HTTP_403_FORBIDDEN,
                )

            now = timezone.now()
            is_group_call = call.conversation.type == Conversation.GROUP
            is_caller = request.user.id == call.caller_id

            if action == "accept":
                if call.status in {
                    CallSession.ENDED,
                    CallSession.REJECTED,
                    CallSession.MISSED,
                    CallSession.CANCELLED,
                }:
                    return Response(
                        {"error": "This call is no longer active"},
                        status=status.HTTP_409_CONFLICT,
                    )

                participant.status = CallParticipant.JOINED
                participant.joined_at = participant.joined_at or now
                participant.left_at = None
                participant.save(
                    update_fields=[
                        "status",
                        "joined_at",
                        "left_at",
                    ]
                )

                if not is_caller and call.status == CallSession.RINGING:
                    call.status = CallSession.ACCEPTED
                    call.answered_at = now
                    call.save(update_fields=["status", "answered_at"])

            elif action == "reject":
                if is_caller:
                    return Response(
                        {
                            "error": (
                                "The caller must cancel or end the call"
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                participant.status = CallParticipant.DECLINED
                participant.left_at = now
                participant.save(update_fields=["status", "left_at"])

                if not is_group_call:
                    call.status = CallSession.REJECTED
                    call.ended_at = now
                    call.save(update_fields=["status", "ended_at"])
                else:
                    remaining = CallParticipant.objects.filter(
                        call=call,
                    ).exclude(
                        user_id=call.caller_id,
                    ).filter(
                        status__in=[
                            CallParticipant.RINGING,
                            CallParticipant.JOINED,
                        ]
                    ).exists()

                    if not remaining and call.status == CallSession.RINGING:
                        call.status = CallSession.REJECTED
                        call.ended_at = now
                        call.save(update_fields=["status", "ended_at"])

            elif action == "missed":
                if is_caller:
                    return Response(
                        {"error": "The caller cannot mark the call missed"},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                participant.status = CallParticipant.MISSED
                participant.left_at = now
                participant.save(update_fields=["status", "left_at"])

                if not is_group_call:
                    call.status = CallSession.MISSED
                    call.ended_at = now
                    call.save(update_fields=["status", "ended_at"])
                else:
                    remaining = CallParticipant.objects.filter(
                        call=call,
                    ).exclude(
                        user_id=call.caller_id,
                    ).filter(
                        status__in=[
                            CallParticipant.RINGING,
                            CallParticipant.JOINED,
                        ]
                    ).exists()

                    if not remaining and call.status == CallSession.RINGING:
                        call.status = CallSession.MISSED
                        call.ended_at = now
                        call.save(update_fields=["status", "ended_at"])

            elif action == "cancel":
                if not is_caller:
                    return Response(
                        {"error": "Only the caller can cancel the call"},
                        status=status.HTTP_403_FORBIDDEN,
                    )

                if call.status != CallSession.RINGING:
                    return Response(
                        {"error": "Only a ringing call can be cancelled"},
                        status=status.HTTP_409_CONFLICT,
                    )

                call.status = CallSession.CANCELLED
                call.ended_at = now
                call.save(update_fields=["status", "ended_at"])

                CallParticipant.objects.filter(
                    call=call,
                ).exclude(
                    status__in=[
                        CallParticipant.DECLINED,
                        CallParticipant.MISSED,
                    ]
                ).update(
                    status=CallParticipant.LEFT,
                    left_at=now,
                )
                participant.status = CallParticipant.LEFT
                participant.left_at = now

            elif action in {"ended", "leave"}:

                # ========================================================
                # GROUP CALL
                # ========================================================
                if is_group_call:

                    # In a group call there is no special "owner" once
                    # the call is running.
                    #
                    # Caller leaving should behave exactly like any
                    # other participant leaving.
                    participant.status = CallParticipant.LEFT
                    participant.left_at = now

                    participant.save(
                        update_fields=[
                            "status",
                            "left_at",
                        ]
                    )

                    # Check whether anyone is still actually inside
                    # the group call.
                    anyone_still_joined = (
                        CallParticipant.objects
                        .filter(
                            call=call,
                            status=CallParticipant.JOINED,
                        )
                        .exists()
                    )

                    # Only end the CallSession when EVERYONE has left.
                    if not anyone_still_joined:
                        call.status = CallSession.ENDED
                        call.ended_at = now

                        call.save(
                            update_fields=[
                                "status",
                                "ended_at",
                            ]
                        )


                # ========================================================
                # PRIVATE CALL
                # ========================================================
                else:
                    call.status = CallSession.ENDED
                    call.ended_at = now

                    call.save(
                        update_fields=[
                            "status",
                            "ended_at",
                        ]
                    )

                    CallParticipant.objects.filter(
                        call=call,
                    ).update(
                        status=CallParticipant.LEFT,
                        left_at=now,
                    )

                    participant.status = CallParticipant.LEFT
                    participant.left_at = now

        event_data = {
            "type": "call_status_updated",
            **_serialize_call(call),
            "action": action,
            "updated_by": request.user.id,
            "participant_status": participant.status,
        }

        _broadcast_call_event(call.conversation_id, event_data)

        return Response(
            {
                "success": True,
                **_serialize_call(call),
                "participant_status": participant.status,
            }
        )

class ActiveGroupCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, conversation_id):

        membership = (
            ConversationMember.objects
            .select_related("conversation")
            .filter(
                conversation_id=conversation_id,
                user=request.user,
                is_blocked=False,
            )
            .first()
        )

        if not membership:
            return Response(
                {"error": "You are not a member of this group"},
                status=status.HTTP_403_FORBIDDEN,
            )

        conversation = membership.conversation

        if conversation.type != Conversation.GROUP:
            return Response(
                {"error": "This is not a group conversation"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        call = (
            CallSession.objects
            .filter(
                conversation=conversation,
                status__in=[
                    CallSession.RINGING,
                    CallSession.ACCEPTED,
                ],
            )
            .select_related(
                "caller",
                "conversation",
            )
            .first()
        )

        if not call:
            return Response(
                {
                    "active": False,
                    "call": None,
                },
                status=status.HTTP_200_OK,
            )

        # User may have been added to the group after
        # the call originally started.
        participant, _ = CallParticipant.objects.get_or_create(
            call=call,
            user=request.user,
            defaults={
                "status": CallParticipant.INVITED,
            },
        )

        participants = []

        for item in (
            CallParticipant.objects
            .filter(call=call)
            .select_related("user")
        ):
            participants.append({
                "user_id": item.user_id,
                "name": _user_display_name(item.user),
                "profile_picture": _user_avatar_url(
                    request,
                    item.user,
                ),
                "status": item.status,
                "joined_at": (
                    item.joined_at.isoformat()
                    if item.joined_at
                    else None
                ),
                "left_at": (
                    item.left_at.isoformat()
                    if item.left_at
                    else None
                ),
            })

        return Response(
            {
                "active": True,

                "call": {
                    **_serialize_call(call),

                    "livekit_room_name":
                        call.livekit_room_name,

                    "conversation_name":
                        conversation.name or "",

                    "my_participant_status":
                        participant.status,

                    "can_join": True,

                    "participants":
                        participants,
                },
            },
            status=status.HTTP_200_OK,
        )