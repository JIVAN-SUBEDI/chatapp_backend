import mimetypes

from django.contrib.auth import get_user_model
from django.db.models import Count
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Conversation, ConversationMember, Message
from .serializers import ConversationSerializer, MessageSerializer
from django.shortcuts import render
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
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

        if int(other_user_id) == request.user.id:
            return Response({"error": "You cannot chat with yourself"}, status=400)

        try:
            other_user = User.objects.get(id=other_user_id)
        except User.DoesNotExist:
            return Response({"error": "User not found"}, status=404)

        existing = Conversation.objects.filter(
            type=Conversation.PRIVATE,
            members__user=request.user
        ).filter(
            members__user=other_user
        ).annotate(
            member_count=Count("members")
        ).filter(member_count=2).first()

        if existing:
            return Response(ConversationSerializer(existing).data)

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

            return Response({
                "id": user.id,
                "name": getattr(user, "full_name", ""),
                # "username": getattr(user, "username", ""),
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
            return Response({"error": "Only admin can block members"}, status=403)

        member = ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id
        ).first()

        if not member:
            return Response({"error": "Member not found"}, status=404)

        member.is_blocked = True
        member.blocked_by = request.user
        member.save()

        return Response({"success": True})