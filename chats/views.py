import mimetypes

from django.contrib.auth import get_user_model
from django.db.models import Count
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Conversation, ConversationMember, Message,UserFCMToken,CallSession
from .serializers import ConversationSerializer, MessageSerializer
from django.shortcuts import render
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from .firebase import send_incoming_call_push
from django.utils import timezone

User = get_user_model()


def detect_message_type(file):
    if not file:
        return Message.TEXT

    mime_type, _ = mimetypes.guess_type(file.name)

    if not mime_type:
        return Message.FILE

    if mime_type.startswith("image"):
        return Message.IMAGE
    if mime_type.startswith("video"):
        return Message.VIDEO
    if mime_type.startswith("audio"):
        return Message.AUDIO

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
        name = request.data.get("name")
        member_ids = request.data.get("member_ids", [])

        if not name:
            return Response({"error": "Group name is required"}, status=400)

        conversation = Conversation.objects.create(
            type=Conversation.GROUP,
            name=name,
            image=request.FILES.get("image"),
            created_by=request.user
        )

        ConversationMember.objects.create(
            conversation=conversation,
            user=request.user,
            is_admin=True
        )

        users = User.objects.filter(id__in=member_ids)

        for user in users:
            if user != request.user:
                ConversationMember.objects.get_or_create(
                    conversation=conversation,
                    user=user
                )

        return Response(
            ConversationSerializer(conversation).data,
            status=status.HTTP_201_CREATED
        )
# Add this view in your chat/views.py

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
            user=self.request.user
        ).exists()

        if not is_member:
            return Message.objects.none()

        return Message.objects.filter(
            conversation_id=conversation_id
        )




class SendMessageView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, conversation_id):
        member = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user=request.user
        ).first()

        if not member:
            return Response({"error": "You are not a member"}, status=403)

        if member.is_blocked:
            return Response(
                {"error": "You are blocked in this chat"},
                status=403
            )

        conversation = member.conversation

        media = request.FILES.get("media")
        message_type = request.data.get("message_type")

        if not message_type:
            message_type = detect_message_type(media)
        
        # message = request.data.get("text",)

        message = Message.objects.create(
            conversation=conversation,
            sender=request.user,
            message_type=message_type,
            text=request.data.get("text", ""),
            media=media,
            thumbnail=request.FILES.get("thumbnail"),
            file_name=media.name if media else None,
            file_size=media.size if media else None,
            mime_type=media.content_type if media else None,
            duration=request.data.get("duration") or None,
            reply_to_id=request.data.get("reply_to") or None,
            reaction_to_id=request.data.get("reaction_to") or None,
            reaction=request.data.get("reaction") or None,
        )

        message.read_by.add(request.user)

        conversation.save()

        data = MessageSerializer(message, context={"request": request}).data

        channel_layer = get_channel_layer()

        async_to_sync(channel_layer.group_send)(
            f"chat_{conversation_id}",
            {
                "type": "chat_message",
                "message": data,
            }
        )

        return Response(data, status=status.HTTP_201_CREATED)


class EditMessageView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, message_id):
        try:
            message = Message.objects.get(id=message_id, sender=request.user)
        except Message.DoesNotExist:
            return Response({"error": "Message not found"}, status=404)

        if message.is_deleted:
            return Response({"error": "Message is deleted"}, status=400)

        message.text = request.data.get("text", message.text)
        message.is_edited = True
        message.save()

        return Response(MessageSerializer(message).data)


class DeleteMessageView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request, message_id):
        try:
            message = Message.objects.get(id=message_id, sender=request.user)
        except Message.DoesNotExist:
            return Response({"error": "Message not found"}, status=404)

        message.text = ""
        message.media = None
        message.thumbnail = None
        message.is_deleted = True
        message.save()

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


class StartCallView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        receiver_id = request.data.get("receiver_id")
        conversation_id = request.data.get("conversation_id")
        is_video_call = request.data.get("is_video_call", False)

        if not receiver_id or not conversation_id:
            return Response(
                {"error": "receiver_id and conversation_id are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            receiver = User.objects.get(id=receiver_id)
        except User.DoesNotExist:
            return Response(
                {"error": "Receiver not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        call = CallSession.objects.create(
            conversation_id=conversation_id,
            caller=request.user,
            receiver=receiver,
            call_type=CallSession.VIDEO if is_video_call else CallSession.AUDIO,
        )

        caller_name = (
            getattr(request.user, "full_name", None)
            or getattr(request.user, "username", "")
            or str(request.user.id)
        )

        caller_avatar = (
            getattr(request.user, "avatar_url", None)
            or getattr(request.user, "avatar", "")
            or ""
        )

        call_data = {
            "call_id": call.id,
            "conversation_id": conversation_id,
            "caller_id": request.user.id,
            "caller_name": caller_name,
            "caller_avatar": str(caller_avatar),
            "is_video_call": bool(is_video_call),
        }

        tokens = UserFCMToken.objects.filter(
            user=receiver,
            is_active=True,
        )

        sent = 0

        for item in tokens:
            try:
                send_incoming_call_push(
                    token=item.token,
                    call_data=call_data,
                )
                sent += 1
            except Exception:
                item.is_active = False
                item.save(update_fields=["is_active"])

        return Response(
            {
                "success": True,
                "call_id": call.id,
                "push_sent": sent,
            }
        )


class UpdateCallStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, call_id):
        action = request.data.get("action")

        try:
            call = CallSession.objects.get(id=call_id)
        except CallSession.DoesNotExist:
            return Response(
                {"error": "Call not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if request.user not in [call.caller, call.receiver]:
            return Response(
                {"error": "Not allowed"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if action == "accept":
            call.status = CallSession.ACCEPTED
            call.answered_at = timezone.now()

        elif action == "reject":
            call.status = CallSession.REJECTED
            call.ended_at = timezone.now()

        elif action == "end":
            call.status = CallSession.ENDED
            call.ended_at = timezone.now()

        elif action == "missed":
            call.status = CallSession.MISSED
            call.ended_at = timezone.now()

        else:
            return Response(
                {"error": "Invalid action"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        call.save()

        return Response({"success": True, "status": call.status})