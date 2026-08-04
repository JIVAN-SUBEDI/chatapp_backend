from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q
import uuid

User = settings.AUTH_USER_MODEL


class Conversation(models.Model):
    PRIVATE = "private"
    GROUP = "group"

    TYPE_CHOICES = (
        (PRIVATE, "Private"),
        (GROUP, "Group"),
    )

    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    name = models.CharField(max_length=255, blank=True, null=True)
    image = models.ImageField(upload_to="chat/groups/", blank=True, null=True)

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_conversations"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name or f"{self.type} chat {self.id}"


class ConversationMember(models.Model):
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="members"
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="chat_memberships"
    )

    is_admin = models.BooleanField(default=False)
    is_muted = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)
    nickname = models.CharField(max_length=100, blank=True, null=True)
    is_blocked = models.BooleanField(default=False)
    blocked_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="blocked_chat_members"
    )
    class Meta:
        unique_together = ("conversation", "user")

    def __str__(self):
        return f"{self.user} in {self.conversation}"



class Message(models.Model):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"
    REPLY = "reply"
    REACTION = "reaction"

    MESSAGE_TYPES = (
        (TEXT, "Text"),
        (IMAGE, "Image"),
        (VIDEO, "Video"),
        (AUDIO, "Audio"),
        (FILE, "File"),
        (REPLY, "Reply"),
        (REACTION, "Reaction"),
    )

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="messages"
    )

    sender = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_messages"
    )

    message_type = models.CharField(
        max_length=20,
        choices=MESSAGE_TYPES,
        default=TEXT
    )

    text = models.TextField(blank=True)

    media = models.FileField(
        upload_to="chat/media/",
        blank=True,
        null=True
    )

    thumbnail = models.ImageField(
        upload_to="chat/thumbnails/",
        blank=True,
        null=True
    )

    file_name = models.CharField(max_length=255, blank=True, null=True)
    file_size = models.BigIntegerField(blank=True, null=True)
    mime_type = models.CharField(max_length=120, blank=True, null=True)
    duration = models.FloatField(blank=True, null=True)

    reply_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reply_messages"
    )

    reaction_to = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reaction_messages"
    )

    reaction = models.CharField(max_length=30, blank=True, null=True)

    is_edited = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    read_by = models.ManyToManyField(
        User,
        blank=True,
        related_name="read_messages"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.sender} - {self.message_type}"
    
class MessageAttachment(models.Model):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"

    ATTACHMENT_TYPES = (
        (IMAGE, "Image"),
        (VIDEO, "Video"),
        (AUDIO, "Audio"),
        (FILE, "File"),
    )

    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="attachments",
    )

    file = models.FileField(
        upload_to="chat/attachments/",
    )

    thumbnail = models.ImageField(
        upload_to="chat/thumbnails/",
        blank=True,
        null=True,
    )

    attachment_type = models.CharField(
        max_length=20,
        choices=ATTACHMENT_TYPES,
        default=FILE,
    )

    file_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
    )

    file_size = models.BigIntegerField(
        blank=True,
        null=True,
    )

    mime_type = models.CharField(
        max_length=120,
        blank=True,
        null=True,
    )

    duration = models.FloatField(
        blank=True,
        null=True,
    )

    order = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "created_at"]

    def __str__(self):
        return f"{self.attachment_type} attachment for message {self.message_id}"
class UserFCMToken(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="fcm_tokens",
    )
    token = models.TextField(unique=True)
    device_id = models.CharField(max_length=255, blank=True, null=True)
    platform = models.CharField(max_length=30, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user_id} - {self.platform}"


class CallSession(models.Model):
    AUDIO = "audio"
    VIDEO = "video"

    CALL_TYPES = (
        (AUDIO, "Audio"),
        (VIDEO, "Video"),
    )

    RINGING = "ringing"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ENDED = "ended"
    MISSED = "missed"
    CANCELLED = "cancelled"

    STATUS_CHOICES = (
        (RINGING, "Ringing"),
        (ACCEPTED, "Accepted"),
        (REJECTED, "Rejected"),
        (ENDED, "Ended"),
        (MISSED, "Missed"),
        (CANCELLED, "Cancelled"),
    )

    # Keep the normal integer database ID.
    # Use this UUID in APIs and WebSocket events.
    call_uuid = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        db_index=True,
    )

    # This replaces conversation_id = models.IntegerField().
    # Django still allows:
    # CallSession.objects.create(conversation_id=conversation_id)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="call_sessions",
    )

    caller = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="outgoing_calls",
    )

    # Receiver is used only for private calls.
    # For group calls, participants are stored in CallParticipant.
    receiver = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="incoming_calls",
        blank=True,
        null=True,
    )

    call_type = models.CharField(
        max_length=10,
        choices=CALL_TYPES,
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=RINGING,
        db_index=True,
    )

    # Every participant in a group call joins this same LiveKit room.
    livekit_room_name = models.CharField(
        max_length=255,
        unique=True,
        blank=True,
        null=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # For a group call, set this when the first participant joins.
    answered_at = models.DateTimeField(
        blank=True,
        null=True,
    )

    ended_at = models.DateTimeField(
        blank=True,
        null=True,
    )

    class Meta:
        ordering = ["-created_at"]

        indexes = [
            models.Index(
                fields=["conversation", "status"],
                name="call_conversation_status_idx",
            ),
            models.Index(
                fields=["caller", "created_at"],
                name="call_caller_created_idx",
            ),
        ]

        constraints = [
            # Prevent the same conversation from having multiple
            # ringing or accepted calls simultaneously.
            models.UniqueConstraint(
                fields=["conversation"],
                condition=Q(
                    status__in=[
                        "ringing",
                        "accepted",
                    ]
                ),
                name="one_active_call_per_conversation",
            ),
        ]

    @property
    def is_group_call(self):
        return self.conversation.type == Conversation.GROUP

    @property
    def is_private_call(self):
        return self.conversation.type == Conversation.PRIVATE

    def clean(self):
        super().clean()

        if not self.conversation_id:
            return

        if self.conversation.type == Conversation.PRIVATE:
            if not self.receiver_id:
                raise ValidationError(
                    {
                        "receiver": (
                            "A receiver is required for a private call."
                        )
                    }
                )

            if self.caller_id == self.receiver_id:
                raise ValidationError(
                    {
                        "receiver": (
                            "The caller and receiver cannot be the same user."
                        )
                    }
                )

        elif self.conversation.type == Conversation.GROUP:
            if self.receiver_id:
                raise ValidationError(
                    {
                        "receiver": (
                            "Do not set a receiver for a group call. "
                            "Use CallParticipant instead."
                        )
                    }
                )

    def save(self, *args, **kwargs):
        if not self.livekit_room_name:
            self.livekit_room_name = (
                f"hiddenly-call-{uuid.uuid4().hex}"
            )

        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        call_kind = (
            "Group"
            if self.is_group_call
            else "Private"
        )

        return (
            f"{call_kind} {self.call_type} call "
            f"{self.call_uuid} - {self.status}"
        )


class CallParticipant(models.Model):
    INVITED = "invited"
    RINGING = "ringing"
    JOINED = "joined"
    DECLINED = "declined"
    LEFT = "left"
    MISSED = "missed"

    PARTICIPANT_STATUS_CHOICES = (
        (INVITED, "Invited"),
        (RINGING, "Ringing"),
        (JOINED, "Joined"),
        (DECLINED, "Declined"),
        (LEFT, "Left"),
        (MISSED, "Missed"),
    )

    call = models.ForeignKey(
        CallSession,
        on_delete=models.CASCADE,
        related_name="participants",
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="call_participations",
    )

    status = models.CharField(
        max_length=20,
        choices=PARTICIPANT_STATUS_CHOICES,
        default=INVITED,
        db_index=True,
    )

    invited_at = models.DateTimeField(auto_now_add=True)

    joined_at = models.DateTimeField(
        blank=True,
        null=True,
    )

    left_at = models.DateTimeField(
        blank=True,
        null=True,
    )

    # These fields store the participant's last known media state.
    is_microphone_enabled = models.BooleanField(default=True)
    is_camera_enabled = models.BooleanField(default=False)

    class Meta:
        ordering = ["invited_at"]

        constraints = [
            models.UniqueConstraint(
                fields=["call", "user"],
                name="unique_user_per_call",
            ),
        ]

        indexes = [
            models.Index(
                fields=["call", "status"],
                name="participant_call_status_idx",
            ),
            models.Index(
                fields=["user", "status"],
                name="participant_user_status_idx",
            ),
        ]

    def clean(self):
        super().clean()

        if not self.call_id or not self.user_id:
            return

        is_conversation_member = (
            ConversationMember.objects.filter(
                conversation_id=self.call.conversation_id,
                user_id=self.user_id,
                is_blocked=False,
            ).exists()
        )

        if not is_conversation_member:
            raise ValidationError(
                {
                    "user": (
                        "This user is not an active member of "
                        "the conversation."
                    )
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"User {self.user_id} in call "
            f"{self.call.call_uuid} - {self.status}"
        )