import json
import uuid

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.layers import get_channel_layer
from django.contrib.auth import get_user_model

from .models import Conversation, ConversationMember, Message

User = get_user_model()


def user_group(user_id):
    return f"user_{user_id}"


# ─────────────────────────── Call event names ───────────────────────────
CALL_OFFER = "call_offer"
CALL_ANSWER = "call_answer"
ICE_CANDIDATE = "ice_candidate"
CALL_READY = "call_ready"
CALL_REJECT = "call_reject"
CALL_END = "call_end"
CALL_BUSY = "call_busy"
CALL_TIMEOUT = "call_timeout"
CALL_RENEGOTIATE_OFFER = "call_renegotiate_offer"
CALL_RENEGOTIATE_ANSWER = "call_renegotiate_answer"
CALL_VIDEO_TOGGLE = "call_video_toggle"
CALL_VIDEO_UPGRADE_REJECTED = "call_video_upgrade_rejected"

CALL_EVENTS = {
    CALL_OFFER, CALL_ANSWER, ICE_CANDIDATE, CALL_READY, CALL_REJECT,
    CALL_END, CALL_BUSY, CALL_TIMEOUT, CALL_RENEGOTIATE_OFFER,
    CALL_RENEGOTIATE_ANSWER, CALL_VIDEO_TOGGLE, CALL_VIDEO_UPGRADE_REJECTED,
}
CALL_STOP_EVENTS = {CALL_REJECT, CALL_END, CALL_TIMEOUT}


# ─────────────────── Helper for views / REST / Celery ───────────────────
def notify_users(user_ids, event, data, conversation_id=None):
    """
    Push an event to users from normal Django code, e.g. after creating a
    conversation or uploading a media message through REST:

        notify_users(member_ids, "conversation_created", serializer.data)
        notify_users(member_ids, "new_message", msg_data, conversation_id=conv.id)
    """
    layer = get_channel_layer()
    msg = {
        "event": event,
        "type": event,
        "conversation_id": str(conversation_id) if conversation_id else None,
        "data": data,
    }
    for uid in user_ids:
        async_to_sync(layer.group_send)(
            user_group(uid), {"type": "deliver", "data": msg}
        )


class RealtimeConsumer(AsyncWebsocketConsumer):

    # ═════════════════════════ CONNECT / DISCONNECT ═════════════════════════
    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            await self.close()
            return

        uid = self.user.id
        self.joined_groups = [
            user_group(uid),
            f"user_chat_{uid}",   # old group names → existing views still work
            f"user_call_{uid}",
        ]
        for g in self.joined_groups:
            await self.channel_layer.group_add(g, self.channel_name)

        await self.accept()
        await self.send_json({
            "event": "connected",
            "type": "connected",
            "payload": {"user_id": str(uid)},
        })

    async def disconnect(self, close_code):
        for g in getattr(self, "joined_groups", []):
            await self.channel_layer.group_discard(g, self.channel_name)

    # ═════════════════════════════ CLIENT → SERVER ═════════════════════════════
    async def receive(self, text_data=None, bytes_data=None):
        try:
            data = json.loads(text_data or "{}")
        except json.JSONDecodeError:
            await self.send_error("Invalid JSON")
            return

        event = str(
            data.get("event") or data.get("type") or data.get("action") or ""
        ).strip().lower()
        conversation_id = data.get("conversation_id") or data.get("conversationId")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = data  # allow flat messages too

        if event == "ping":
            await self.send_json({"event": "pong", "type": "pong", "action": "pong"})
            return
        if event == "pong":
            return

        if event in CALL_EVENTS:
            await self.handle_call(event, data, payload, conversation_id)
            return

        handlers = {
            "send_message": self.handle_send_message,
            "typing": self.handle_typing,
            "read_message": self.handle_read,
            "edit_message": self.handle_edit,
            "delete_message": self.handle_delete,
        }
        handler = handlers.get(event)
        if handler is None:
            await self.send_error("Unknown event", event=event)
            return

        if not conversation_id or not await self.is_member(conversation_id):
            await self.send_error("Not a member of this conversation", event=event)
            return

        await handler(str(conversation_id), payload)

    # ─────────────────────────────── Chat ───────────────────────────────
    async def handle_send_message(self, conv_id, p):
        message = await self.save_message(conv_id, p)

        # 1. ack to the sending device → "sending" becomes "sent ✓"
        await self.send_json({
            "event": "message_ack",
            "type": "message_ack",
            "conversation_id": conv_id,
            "data": {
                "client_id": p.get("client_id") or p.get("clientId"),
                "message": message,
            },
        })

        # 2. everyone in the conversation (incl. my other devices, not this one)
        await self.fan_out(
            conv_id,
            self.envelope("new_message", conv_id, message),
            include_self=True,
            exclude_channel=self.channel_name,
        )

    async def handle_typing(self, conv_id, p):
        is_typing = str(p.get("is_typing", False)).lower() == "true"
        await self.fan_out(
            conv_id,
            self.envelope("typing", conv_id, {
                "user_id": str(self.user.id),
                "is_typing": is_typing,
            }),
        )

    async def handle_read(self, conv_id, p):
        message_id = p.get("message_id")
        if not await self.mark_read(conv_id, message_id):
            await self.send_error("Message not found", event="read_message")
            return
        await self.fan_out(
            conv_id,
            self.envelope("message_read", conv_id, {
                "message_id": message_id,
                "user_id": str(self.user.id),
            }),
            include_self=True,                  # clears unread on my other devices
            exclude_channel=self.channel_name,
        )

    async def handle_edit(self, conv_id, p):
        result = await self.edit_message(conv_id, p.get("message_id"), p.get("text", ""))
        if "error" in result:
            await self.send_error(result["error"], event="edit_message")
            return
        await self.fan_out(
            conv_id,
            self.envelope("message_edited", conv_id, result),
            include_self=True,
            exclude_channel=self.channel_name,
        )

    async def handle_delete(self, conv_id, p):
        result = await self.delete_message(conv_id, p.get("message_id"))
        if "error" in result:
            await self.send_error(result["error"], event="delete_message")
            return
        await self.fan_out(
            conv_id,
            self.envelope("message_deleted", conv_id, result),
            include_self=True,
            exclude_channel=self.channel_name,
        )

    # ─────────────────────────────── Calls ───────────────────────────────
    async def handle_call(self, event, data, payload, conversation_id):
        me = str(self.user.id)

        if conversation_id and not await self.is_member(conversation_id):
            await self.send_error("Not a member of this conversation", event=event)
            return

        target = (
            data.get("target_user") or data.get("targetUser")
            or data.get("target_user_id") or data.get("targetUserId")
            or payload.get("target_user") or payload.get("targetUser")
        )
        if not target and conversation_id:
            target = await self.get_other_member_user_id(conversation_id, me)
        if not target:
            await self.send_error("target_user is required", event=event)
            return
        target = str(target)

        call_id = str(
            payload.get("call_id") or payload.get("callId")
            or data.get("call_id") or data.get("callId")
            or uuid.uuid4()
        )
        conv = str(conversation_id) if conversation_id else None

        payload = dict(payload)
        for k, v in {
            "from": me, "from_user": me, "caller_id": me, "callerId": me,
            "conversation_id": conv, "conversationId": conv,
            "call_id": call_id, "callId": call_id,
            "target_user": target, "targetUser": target,
        }.items():
            payload.setdefault(k, v)

        # 1. the signal itself → all devices of the target
        await self.send_to_user(target, {
            "event": event,
            "type": event,
            "from_user": me,
            "target_user": target,
            "conversation_id": conv,
            "payload": payload,
        })

        # 2. ring the target
        if event == CALL_OFFER:
            is_video = str(
                payload.get("is_video_call", payload.get("isVideoCall", False))
            ).lower() == "true"
            name = await self.get_user_display_name(me)
            avatar = await self.get_user_avatar(me)
            await self.send_to_user(target, {
                "event": "incoming_call",
                "type": "incoming_call",
                "payload": {
                    "conversation_id": conv, "conversationId": conv,
                    "caller_id": me, "callerId": me,
                    "from": me, "from_user": me,
                    "target_user": target, "targetUser": target,
                    "caller_name": name, "callerName": name,
                    "caller_avatar": avatar, "callerAvatar": avatar,
                    "is_video_call": is_video, "isVideoCall": is_video,
                    "call_id": call_id, "callId": call_id,
                    "offer": payload.get("offer"),
                },
            })

        # 3. stop ringing
        if event in CALL_STOP_EVENTS:
            await self.send_to_user(target, {
                "event": "call_cancelled",
                "type": "call_cancelled",
                "payload": {
                    "conversation_id": conv, "call_id": call_id, "callId": call_id,
                    "from": me, "from_user": me, "reason": event,
                },
            })

        # 4. answered on my phone → stop ringing on my tablet / web
        if event == CALL_ANSWER:
            await self.send_to_user(me, {
                "event": "call_answered_elsewhere",
                "type": "call_answered_elsewhere",
                "payload": {"call_id": call_id, "conversation_id": conv},
            }, exclude_channel=self.channel_name)

        # 5. confirmation back to the sender
        await self.send_json({
            "event": "call_event_sent",
            "type": "call_event_sent",
            "payload": {
                "sent_event": event, "from_user": me, "target_user": target,
                "conversation_id": conv, "call_id": call_id, "callId": call_id,
            },
        })

    # ═════════════════════════════ ROUTING HELPERS ═════════════════════════════
    @staticmethod
    def envelope(event, conv_id, data):
        return {"event": event, "type": event, "conversation_id": conv_id, "data": data}

    async def send_to_user(self, user_id, data, exclude_channel=None):
        await self.channel_layer.group_send(
            user_group(user_id),
            {"type": "deliver", "data": data, "exclude_channel": exclude_channel},
        )

    async def fan_out(self, conv_id, data, include_self=False, exclude_channel=None):
        """Send `data` to every member of the conversation (each on all devices)."""
        me = str(self.user.id)
        for uid in await self.member_ids(conv_id):
            if uid == me and not include_self:
                continue
            await self.send_to_user(uid, data, exclude_channel=exclude_channel)

    # ═══════════════════════ SERVER → CLIENT (group handlers) ═══════════════════════
    async def deliver(self, event):
        if event.get("exclude_channel") == self.channel_name:
            return
        await self.send_json(event["data"])

    # Old group_send types used by existing views (kept for compatibility)
    async def conversation_created(self, event):
        await self.send_json({"event": "conversation_created", "type": "conversation_created", "data": event["data"]})

    async def conversation_updated(self, event):
        await self.send_json({"event": "conversation_updated", "type": "conversation_updated", "data": event["data"]})

    async def conversation_deleted(self, event):
        await self.send_json({"event": "conversation_deleted", "type": "conversation_deleted", "data": event["data"]})

    async def new_message(self, event):
        await self.send_json({"event": "new_message", "type": "new_message", "data": event["data"]})

    async def incoming_call(self, event):
        await self.send_json(event.get("data") or {})

    async def call_cancelled(self, event):
        await self.send_json(event.get("data") or {})

    # ═════════════════════════════════ SENDING ═════════════════════════════════
    async def send_json(self, data):
        await self.send(text_data=json.dumps(data, default=str))

    async def send_error(self, message, **extra):
        await self.send_json({"event": "error", "type": "error", "error": message, **extra})

    # ═════════════════════════════════ DATABASE ═════════════════════════════════
    @database_sync_to_async
    def is_member(self, conversation_id):
        return ConversationMember.objects.filter(
            conversation_id=conversation_id, user_id=self.user.id
        ).exists()

    @database_sync_to_async
    def member_ids(self, conversation_id):
        return [
            str(uid) for uid in ConversationMember.objects
            .filter(conversation_id=conversation_id)
            .values_list("user_id", flat=True)
        ]

    @database_sync_to_async
    def get_other_member_user_id(self, conversation_id, current_user_id):
        member = (
            ConversationMember.objects
            .filter(conversation_id=conversation_id)
            .exclude(user_id=current_user_id)
            .first()
        )
        return str(member.user_id) if member else None

    @database_sync_to_async
    def save_message(self, conversation_id, data):
        message = Message.objects.create(
            conversation_id=conversation_id,
            sender=self.user,
            message_type=data.get("message_type", Message.TEXT),
            text=data.get("text", ""),
            reply_to_id=data.get("reply_to"),
            reaction_to_id=data.get("reaction_to"),
            reaction=data.get("reaction"),
        )
        message.read_by.add(self.user)
        Conversation.objects.get(id=conversation_id).save()  # bump updated_at

        return {
            "id": message.id,
            "conversation": message.conversation_id,
            "client_id": data.get("client_id") or data.get("clientId"),
            "sender": {
                "id": self.user.id,
                "phone": self.user.phone,
                "full_name": self.user.full_name or "",
                "profile_picture": (
                    self.user.profile_picture.url if self.user.profile_picture else None
                ),
            },
            "message_type": message.message_type,
            "text": message.text,
            "reply_to": message.reply_to_id,
            "reaction_to": message.reaction_to_id,
            "reaction": message.reaction,
            "is_edited": message.is_edited,
            "is_deleted": message.is_deleted,
            "created_at": message.created_at.isoformat(),
        }

    @database_sync_to_async
    def mark_read(self, conversation_id, message_id):
        try:
            message = Message.objects.get(id=message_id, conversation_id=conversation_id)
        except (Message.DoesNotExist, ValueError):
            return False
        message.read_by.add(self.user)
        return True

    @database_sync_to_async
    def edit_message(self, conversation_id, message_id, text):
        try:
            message = Message.objects.get(
                id=message_id, sender=self.user, conversation_id=conversation_id
            )
        except (Message.DoesNotExist, ValueError):
            return {"error": "Message not found"}
        message.text = text
        message.is_edited = True
        message.save()
        return {"id": message.id, "text": message.text, "is_edited": True}

    @database_sync_to_async
    def delete_message(self, conversation_id, message_id):
        try:
            message = Message.objects.get(
                id=message_id, sender=self.user, conversation_id=conversation_id
            )
        except (Message.DoesNotExist, ValueError):
            return {"error": "Message not found"}
        message.text = ""
        message.media = None
        message.thumbnail = None
        message.is_deleted = True
        message.save()
        return {"id": message.id, "is_deleted": True}

    @database_sync_to_async
    def get_user_display_name(self, user_id):
        user = User.objects.filter(id=user_id).first()
        if user:
            for field in ["full_name", "name", "username", "phone"]:
                value = getattr(user, field, None)
                if value:
                    return str(value)
        return "Incoming call"

    @database_sync_to_async
    def get_user_avatar(self, user_id):
        user = User.objects.filter(id=user_id).first()
        if not user:
            return ""
        image = (
            getattr(user, "profile_picture", None)
            or getattr(user, "avatar", None)
            or getattr(user, "image", None)
        )
        if not image:
            return ""
        try:
            return image.url
        except Exception:
            return str(image)