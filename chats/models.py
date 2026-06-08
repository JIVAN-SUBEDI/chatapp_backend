from django.conf import settings
from django.db import models

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

    STATUS_CHOICES = (
        (RINGING, "Ringing"),
        (ACCEPTED, "Accepted"),
        (REJECTED, "Rejected"),
        (ENDED, "Ended"),
        (MISSED, "Missed"),
    )

    conversation_id = models.IntegerField()
    caller = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="outgoing_calls",
    )
    receiver = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="incoming_calls",
    )
    call_type = models.CharField(max_length=10, choices=CALL_TYPES)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=RINGING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    answered_at = models.DateTimeField(blank=True, null=True)
    ended_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"{self.caller_id} -> {self.receiver_id} ({self.call_type})"