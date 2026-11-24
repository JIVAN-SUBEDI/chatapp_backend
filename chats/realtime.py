"""
Shared realtime helpers used by views.py, call_service.py and consumers.py.

Everything is pushed to the per-user group `user_<id>` that the global
RealtimeConsumer joins, so every device of every member gets it.
"""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.contrib.auth import get_user_model

from .models import Conversation, ConversationMember, Message, UserFCMToken

User = get_user_model()


def user_group(user_id):
    return f"user_{user_id}"


# ─────────────────────────── sending ───────────────────────────
def frame(event, data, conversation_id=None):
    conv = conversation_id if conversation_id is not None else (
        data.get("conversation_id") if isinstance(data, dict) else None
    )
    return {
        "event": event,
        "type": event,
        "conversation_id": str(conv) if conv is not None else None,
        "data": data,
        "payload": data,
    }


def notify_users(user_ids, event, data, conversation_id=None, exclude_channel=None):
    """Push one event to all devices of the given users (safe from sync code)."""
    layer = get_channel_layer()
    if layer is None:
        return
    msg = frame(event, data, conversation_id)
    for uid in set(str(u) for u in user_ids if u is not None):
        async_to_sync(layer.group_send)(
            user_group(uid),
            {"type": "deliver", "data": msg, "exclude_channel": exclude_channel},
        )


def member_ids(conversation_id, include_blocked=False):
    qs = ConversationMember.objects.filter(conversation_id=conversation_id)
    if not include_blocked:
        qs = qs.filter(is_blocked=False)
    return list(qs.values_list("user_id", flat=True))


def notify_conversation(conversation_id, event, data, exclude_user=None,
                        exclude_channel=None, include_blocked=False):
    """Push an event to every member of a conversation."""
    ids = [u for u in member_ids(conversation_id, include_blocked) if u != exclude_user]
    notify_users(ids, event, data, conversation_id=conversation_id,
                 exclude_channel=exclude_channel)


# ─────────────────────────── users ───────────────────────────
def user_display_name(user):
    for field in ("full_name", "name", "username", "phone"):
        value = getattr(user, field, None)
        if value:
            return str(value)
    return str(getattr(user, "id", "")) or "Hiddenly user"


def absolute_url(path, request=None):
    """Media URLs must be absolute for the app. Without a request (socket,
    Celery) we use settings.PUBLIC_BASE_URL, e.g. "https://api.hiddenly.app"."""
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if request is not None:
        try:
            return request.build_absolute_uri(path)
        except Exception:
            pass
    base = getattr(settings, "PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}{path}" if base else path


def user_avatar_url(user, request=None):
    image = (
        getattr(user, "profile_picture", None)
        or getattr(user, "avatar", None)
        or getattr(user, "image", None)
    )
    if not image:
        return ""
    try:
        return absolute_url(image.url, request)
    except (AttributeError, ValueError):
        return str(image) if image else ""


# ─────────────────────────── messages ───────────────────────────
def serialize_message(message, request=None):
    from .serializers import MessageSerializer  # local import: avoids cycles

    message = (
        Message.objects
        .select_related("sender", "reply_to__sender", "reaction_to__sender")
        .prefetch_related("attachments", "read_by")
        .get(pk=message.pk)
    )
    return MessageSerializer(message, context={"request": request}).data


def push_new_message(message, sender, request=None, media_count=0, detected_types=None):
    """FCM push for a new message to every other (non-blocked) member."""
    from .firebase import send_message_push

    conversation = message.conversation
    sender_name = user_display_name(sender)
    sender_avatar = user_avatar_url(sender, request)

    if media_count > 1:
        detected_types = detected_types or []
        if detected_types and all(k == Message.IMAGE for k in detected_types):
            body = f"Sent {media_count} images"
        elif detected_types and all(k == Message.VIDEO for k in detected_types):
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
        "sender_id": sender.id,
        "sender_name": sender_name,
        "sender_avatar": sender_avatar,
        "message_type": message.message_type,
        "text": message.text or "",
        "attachment_count": media_count,
        "has_multiple_attachments": media_count > 1,
    }

    receiver_ids = (
        ConversationMember.objects
        .filter(conversation=conversation, is_blocked=False)
        .exclude(user=sender)
        .values_list("user_id", flat=True)
    )
    for item in UserFCMToken.objects.filter(user_id__in=receiver_ids, is_active=True).iterator():
        try:
            send_message_push(token=item.token, message_data=message_data)
        except Exception:
            item.is_active = False
            item.save(update_fields=["is_active"])