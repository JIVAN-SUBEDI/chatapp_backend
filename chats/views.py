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
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import call_service as cs
from .models import (
    CallParticipant,
    CallSession,
    Conversation,
    ConversationMember,
    Message,
    MessageAttachment,
    UserFCMToken,
)
from .realtime import (
    notify_conversation,
    notify_users,
    push_new_message,
    user_avatar_url,
    user_display_name,
)
from .serializers import ConversationSerializer, MessageSerializer

User = get_user_model()


# ─────────────────────────── realtime helpers ───────────────────────────
def _conversation_payload(conversation, request):
    return ConversationSerializer(conversation, context={"request": request}).data


def _notify_conversation_changed(conversation, request, event="conversation_updated"):
    """Every member (blocked ones too, so their UI updates) refreshes the chat."""
    notify_conversation(
        conversation.id, event, _conversation_payload(conversation, request),
        include_blocked=True,
    )


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

        data = _conversation_payload(conversation, request)
        notify_users([request.user.id, other_user.id], "conversation_created", data,
                     conversation_id=conversation.id)

        return Response(data, status=status.HTTP_201_CREATED)

         

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
        # REALTIME: NOTIFY EVERY GROUP MEMBER (all their devices)
        # ============================================================

        actual_member_ids = list(
            ConversationMember.objects.filter(
                conversation=conversation,
                is_blocked=False,
            ).values_list("user_id", flat=True)
        )
        notify_users(actual_member_ids, "conversation_created", data,
                     conversation_id=conversation.id)

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
        notify_conversation(conversation.id, "conversation_updated", data)

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
        except (User.DoesNotExist, ValueError, TypeError):
            return Response({"error": "User not found"}, status=404)

        member, created = ConversationMember.objects.get_or_create(
            conversation_id=conversation_id,
            user=user
        )

        conversation = member.conversation
        data = _conversation_payload(conversation, request)
        if created:
            notify_users([user.id], "conversation_created", data,
                         conversation_id=conversation.id)
        notify_conversation(conversation.id, "conversation_updated", data,
                            exclude_user=user.id if created else None)

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

        deleted, _ = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id
        ).delete()

        if deleted:
            notify_users([user_id], "conversation_deleted",
                         {"id": conversation_id, "conversation_id": conversation_id},
                         conversation_id=conversation_id)
            conversation = Conversation.objects.filter(id=conversation_id).first()
            if conversation:
                _notify_conversation_changed(conversation, request)

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

        # Old per-conversation socket group (older app builds).
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"chat_{conversation_id}",
            {
                "type": "chat_message",
                "message": data,
            },
        )

        # Global socket: every member, every device (sender's other devices too).
        client_id = request.data.get("client_id") or request.data.get("clientId")
        notify_conversation(
            conversation.id,
            "new_message",
            {**data, "client_id": client_id} if client_id else data,
        )

        # FCM for members whose app is closed.
        push_new_message(
            message,
            request.user,
            request=request,
            media_count=len(uploaded_files),
            detected_types=[detect_message_type(f) for f in uploaded_files],
        )

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

        notify_conversation(message.conversation_id, "message_edited", {
            "id": message.id,
            "text": message.text,
            "is_edited": True,
            "conversation_id": message.conversation_id,
        })

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

        notify_conversation(message.conversation_id, "message_deleted", {
            "id": message.id,
            "is_deleted": True,
            "conversation_id": message.conversation_id,
        })

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

        # Reading a message means everything before it is read too.
        for older in (
            Message.objects
            .filter(conversation_id=message.conversation_id,
                    created_at__lte=message.created_at)
            .exclude(sender=request.user)
            .exclude(read_by=request.user)[:500]
        ):
            older.read_by.add(request.user)
        message.read_by.add(request.user)

        notify_conversation(message.conversation_id, "message_read", {
            "message_id": message.id,
            "user_id": request.user.id,
            "conversation_id": message.conversation_id,
        })

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

        unread = (
            Message.objects
            .filter(conversation_id=conversation_id)
            .exclude(read_by=request.user)
        )
        for message in unread.iterator():
            message.read_by.add(request.user)

        last = (
            Message.objects.filter(conversation_id=conversation_id)
            .order_by("-created_at").first()
        )
        if last:
            notify_conversation(conversation_id, "message_read", {
                "message_id": last.id,
                "user_id": request.user.id,
                "conversation_id": conversation_id,
            })

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
        _notify_conversation_changed(member.conversation, request)

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
        _notify_conversation_changed(member.conversation, request)

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
        _notify_conversation_changed(conversation, request)

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
        _notify_conversation_changed(conversation, request)

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




# ═════════════════════════════════════════════════════════════════════════
#  CALLS (REST) – thin wrappers around call_service.py
#  The socket (RealtimeConsumer) uses the exact same functions, so a call
#  started over REST rings the new app and vice-versa.
# ═════════════════════════════════════════════════════════════════════════

def _as_boolean(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# Kept for any other module that imported these names from views.
_user_display_name = user_display_name


def _user_avatar_url(request, user):
    return user_avatar_url(user, request)


_get_call_session = cs.get_call
_serialize_call = cs.serialize_call
_broadcast_call_event = cs.broadcast_legacy
_expire_stale_ringing_calls = cs.expire_stale_ringing_calls
CALL_RING_TIMEOUT = cs.CALL_RING_TIMEOUT


def _call_error(exc):
    data = exc.as_dict()
    if "call_status" in data:
        data["status"] = data.pop("call_status")
    return Response(data, status=exc.status)


def _rest(result):
    """REST clients always got integer ids – keep it that way."""
    out = dict(result)
    out.pop("event", None)
    for key in ("call_id", "conversation_id", "caller_id"):
        value = out.get(key)
        if isinstance(value, str) and value.isdigit():
            out[key] = int(value)
    return out


class StartCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            result = cs.start_call(
                request.user,
                request.data.get("conversation_id"),
                is_video=_as_boolean(request.data.get("is_video_call", False)),
                request=request,
                # Old clients expect 409 when a call already exists;
                # send join_if_active=true to join it instead.
                join_existing=_as_boolean(request.data.get("join_if_active", False)),
                receiver_id=request.data.get("receiver_id"),
            )
        except cs.CallError as exc:
            return _call_error(exc)

        if result.get("event") == "call_busy":
            return Response(
                {"error": "User is on another call", "busy": True, **_rest(result)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response({"success": True, **_rest(result)}, status=status.HTTP_201_CREATED)


class LiveKitTokenView(APIView):
    """Join / accept a call and get a LiveKit token."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        call_id = request.data.get("call_id")
        if not call_id:
            return Response({"error": "call_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = cs.join_call(request.user, call_id, request=request, reply_event="call_joined")
        except cs.CallError as exc:
            return _call_error(exc)

        if result.get("event") == "call_ended":
            return Response({"error": "This call is no longer active"},
                            status=status.HTTP_409_CONFLICT)
        return Response(_rest(result), status=status.HTTP_201_CREATED)


class UpdateCallStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    ACTION_ALIASES = {
        "end": "ended", "hangup": "ended", "hang_up": "ended",
        "disconnect": "leave", "disconnected": "leave", "left": "leave",
        "decline": "reject", "declined": "reject",
    }

    def post(self, request, call_id):
        raw_action = str(request.data.get("action", "")).strip().lower()
        action = self.ACTION_ALIASES.get(raw_action, raw_action)

        try:
            if action == "accept":
                result = cs.join_call(request.user, call_id, request=request)
                if result.get("event") == "call_ended":
                    return Response({"error": "This call is no longer active"},
                                    status=status.HTTP_409_CONFLICT)
            elif action == "reject":
                result = cs.decline_call(request.user, call_id,
                                         reason=request.data.get("reason") or "declined")
            elif action == "missed":
                result = cs.decline_call(request.user, call_id, as_missed=True)
            elif action == "cancel":
                result = cs.cancel_call(request.user, call_id,
                                        reason=request.data.get("reason") or "cancelled",
                                        strict=True)
            elif action in {"ended", "leave"}:
                result = cs.leave_call(request.user, call_id)
            else:
                return Response(
                    {"error": "Invalid action", "received_action": raw_action},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except cs.CallError as exc:
            return _call_error(exc)

        return Response({"success": True, "action": action, **_rest(result)},
                        status=status.HTTP_200_OK)


class AddCallParticipantsView(APIView):
    """POST {user_ids: [..]} – ring more people into a running call."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        user_ids = request.data.get("user_ids") or []
        if hasattr(request.data, "getlist") and not isinstance(user_ids, list):
            user_ids = request.data.getlist("user_ids")
        try:
            result = cs.add_to_call(request.user, call_id, user_ids, request=request)
        except cs.CallError as exc:
            return _call_error(exc)
        return Response({"success": True, **_rest(result)})


class ActiveGroupCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, conversation_id):
        membership = (
            ConversationMember.objects.select_related("conversation")
            .filter(conversation_id=conversation_id, user=request.user, is_blocked=False)
            .first()
        )
        if not membership:
            return Response({"error": "You are not a member of this group"},
                            status=status.HTTP_403_FORBIDDEN)

        conversation = membership.conversation
        if conversation.type != Conversation.GROUP:
            return Response({"error": "This is not a group conversation"},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            found = cs.call_status(request.user, conversation_id)
        except cs.CallError as exc:
            return _call_error(exc)
        if not found["call"]:
            return Response({"active": False, "call": None})

        call = cs.get_call(found["call"]["call_id"])
        participant, _ = CallParticipant.objects.get_or_create(
            call=call, user=request.user, defaults={"status": CallParticipant.INVITED},
        )
        participants = [
            {
                "user_id": item.user_id,
                "name": user_display_name(item.user),
                "profile_picture": user_avatar_url(item.user, request),
                "status": item.status,
                "joined_at": item.joined_at.isoformat() if item.joined_at else None,
                "left_at": item.left_at.isoformat() if item.left_at else None,
            }
            for item in CallParticipant.objects.filter(call=call).select_related("user")
        ]
        return Response({
            "active": True,
            "call": {
                **cs.serialize_call(call),
                "livekit_room_name": call.livekit_room_name,
                "conversation_name": conversation.name or "",
                "my_participant_status": participant.status,
                "can_join": True,
                "participants": participants,
            },
        })


@csrf_exempt
def livekit_webhook(request):
    """LiveKit → POST here (set the URL in your LiveKit project's Webhooks).
    Ends calls whose people vanished (app killed, phone died, no network)."""
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        cs.handle_livekit_webhook(
            request.body.decode("utf-8"),
            request.headers.get("Authorization", ""),
        )
    except Exception as exc:  # bad signature or malformed body
        print("LIVEKIT WEBHOOK ERROR:", exc)
        return HttpResponse(status=400)
    return HttpResponse(status=200)