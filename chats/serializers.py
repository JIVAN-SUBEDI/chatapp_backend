from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import Conversation, ConversationMember, Message

User = get_user_model()


class UserMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "phone","full_name","profile_picture"]


class MessageSerializer(serializers.ModelSerializer):
    sender = UserMiniSerializer(read_only=True)
    reply_to_data = serializers.SerializerMethodField()
    reaction_to_data = serializers.SerializerMethodField()
    read_count = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id",
            "conversation",
            "sender",
            "message_type",
            "text",
            "media",
            "thumbnail",
            "file_name",
            "file_size",
            "mime_type",
            "duration",
            "reply_to",
            "reply_to_data",
            "reaction_to",
            "reaction_to_data",
            "reaction",
            "is_edited",
            "is_deleted",
            "read_by",
            "read_count",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "sender",
            "conversation",
            "read_by",
            "is_edited",
            "is_deleted",
        ]

    def get_reply_to_data(self, obj):
        if not obj.reply_to:
            return None
        return {
            "id": obj.reply_to.id,
            "text": obj.reply_to.text,
            "message_type": obj.reply_to.message_type,
            "sender": obj.reply_to.sender.id,
        }

    def get_reaction_to_data(self, obj):
        if not obj.reaction_to:
            return None
        return {
            "id": obj.reaction_to.id,
            "text": obj.reaction_to.text,
            "message_type": obj.reaction_to.message_type,
        }

    def get_read_count(self, obj):
        return obj.read_by.count()


class ConversationMemberSerializer(serializers.ModelSerializer):
    user = UserMiniSerializer(read_only=True)
    display_name = serializers.SerializerMethodField()
    blocked_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ConversationMember
        fields = [
            "id",
            "user",
            "nickname",
            "display_name",
            "is_admin",
            "is_muted",
            "is_blocked",
            "blocked_by",
            "blocked_by_name",
            "joined_at",
        ]

    def get_display_name(self, obj):
        return obj.nickname or obj.user.full_name

    def get_blocked_by_name(self, obj):
        if not obj.blocked_by:
            return None
        return obj.blocked_by.full_name



class ConversationSerializer(serializers.ModelSerializer):
    members = ConversationMemberSerializer(many=True, read_only=True)
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            "id",
            "type",
            "name",
            "image",
            "created_by",
            "members",
            "last_message",
            "created_at",
            "updated_at",
        ]

    def get_last_message(self, obj):
        msg = obj.messages.filter(is_deleted=False).last()
        if not msg:
            return None

        return {
            "id": msg.id,
            "sender": msg.sender.id,
            "message_type": msg.message_type,
            "text": msg.text,
            "media": msg.media.url if msg.media else None,
            "created_at": msg.created_at,
        }