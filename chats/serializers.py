from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    Conversation,
    ConversationMember,
    Message,
    MessageAttachment,
)

User = get_user_model()


class UserMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            "id",
            "phone",
            "full_name",
            "profile_picture",
        ]


class MessageAttachmentSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = MessageAttachment
        fields = [
            "id",
            "file",
            "file_url",
            "thumbnail",
            "thumbnail_url",
            "attachment_type",
            "file_name",
            "file_size",
            "mime_type",
            "duration",
            "order",
            "created_at",
        ]

        read_only_fields = [
            "id",
            "file_url",
            "thumbnail_url",
            "created_at",
        ]

    def build_absolute_url(self, file_field):
        if not file_field:
            return None

        try:
            url = file_field.url
        except (ValueError, AttributeError):
            return None

        request = self.context.get("request")

        if request:
            return request.build_absolute_uri(url)

        return url

    def get_file_url(self, obj):
        return self.build_absolute_url(obj.file)

    def get_thumbnail_url(self, obj):
        return self.build_absolute_url(obj.thumbnail)


class MessageSerializer(serializers.ModelSerializer):
    sender = UserMiniSerializer(read_only=True)

    attachments = MessageAttachmentSerializer(
        many=True,
        read_only=True,
    )

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

            # Old single-file fields
            "media",
            "thumbnail",
            "file_name",
            "file_size",
            "mime_type",
            "duration",

            # New grouped attachment field
            "attachments",

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
            "attachments",
            "read_by",
            "is_edited",
            "is_deleted",
            "created_at",
            "updated_at",
        ]

    def serialize_attachments(self, message):
        return MessageAttachmentSerializer(
            message.attachments.all(),
            many=True,
            context=self.context,
        ).data

    def get_reply_to_data(self, obj):
        replied_message = obj.reply_to

        if not replied_message:
            return None

        return {
            "id": replied_message.id,
            "text": replied_message.text,
            "message_type": replied_message.message_type,
            "sender": replied_message.sender_id,
            "attachments": self.serialize_attachments(
                replied_message
            ),
        }

    def get_reaction_to_data(self, obj):
        reacted_message = obj.reaction_to

        if not reacted_message:
            return None

        return {
            "id": reacted_message.id,
            "text": reacted_message.text,
            "message_type": reacted_message.message_type,
            "sender": reacted_message.sender_id,
            "attachments": self.serialize_attachments(
                reacted_message
            ),
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

        read_only_fields = [
            "id",
            "user",
            "display_name",
            "blocked_by_name",
            "joined_at",
        ]

    def get_display_name(self, obj):
        return (
            obj.nickname
            or getattr(obj.user, "full_name", "")
            or str(obj.user)
        )

    def get_blocked_by_name(self, obj):
        if not obj.blocked_by:
            return None

        return (
            getattr(obj.blocked_by, "full_name", "")
            or str(obj.blocked_by)
        )


class ConversationSerializer(serializers.ModelSerializer):
    members = ConversationMemberSerializer(
        many=True,
        read_only=True,
    )

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

        read_only_fields = [
            "id",
            "created_by",
            "members",
            "last_message",
            "created_at",
            "updated_at",
        ]

    def build_absolute_url(self, file_field):
        if not file_field:
            return None

        try:
            url = file_field.url
        except (ValueError, AttributeError):
            return None

        request = self.context.get("request")

        if request:
            return request.build_absolute_uri(url)

        return url

    def get_last_message(self, obj):
        message = (
            obj.messages
            .filter(is_deleted=False)
            .select_related("sender")
            .prefetch_related("attachments")
            .last()
        )

        if not message:
            return None

        attachments = MessageAttachmentSerializer(
            message.attachments.all(),
            many=True,
            context=self.context,
        ).data

        return {
            "id": message.id,
            "sender": message.sender_id,
            "sender_data": UserMiniSerializer(
                message.sender,
                context=self.context,
            ).data,
            "message_type": message.message_type,
            "text": message.text,

            # Supports old single-image messages
            "media": self.build_absolute_url(message.media),

            # Supports new grouped images
            "attachments": attachments,
            "attachment_count": len(attachments),

            "is_deleted": message.is_deleted,
            "created_at": message.created_at,
        }