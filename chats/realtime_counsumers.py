"""
Hiddenly global realtime socket
================================

One WebSocket per device. Everything goes through it:

  Chat      send_message · typing · read_message · delivered_message
            edit_message · delete_message
  Presence  automatic online / offline + last_seen, presence_query
  Calls     LiveKit 1:1 + group calls (call_start, call_accept, call_decline,
            call_cancel, call_leave, call_add, call_video_request,
            call_video_response, call_status)  → call_service.py (DB-backed,
            the same code the REST call endpoints use)
  Legacy    old WebRTC signalling events (call_offer, call_answer, ...) are
            still relayed unchanged so older app builds keep working.

Client → server frame:
    {"event": "...", "conversation_id": "...", "request_id": "...", "payload": {...}}

Server → client frame:
    {"event": "...", "type": "...", "conversation_id": "...", "request_id": "...",
     "data": {...}, "payload": {...}}          # data == payload, both kept for old clients

Presence lives in the cache → use Redis in production:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                          "LOCATION": "redis://127.0.0.1:6379/1"}}
"""
import json
import uuid

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from . import call_service as cs
from .models import Conversation, ConversationMember, Message
from .realtime import (
    notify_users,          # re-exported: views used to import it from here
    push_new_message,
    serialize_message,
    user_avatar_url,
    user_display_name,
    user_group,
)

User = get_user_model()


# ─────────────────────── Legacy WebRTC call events (relay only) ───────────────────────
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

# ─────────────────────── LiveKit call events (new) ───────────────────────
LK_CALL_EVENTS = {
    "call_start", "call_accept", "call_decline", "call_cancel", "call_leave",
    "call_add", "call_video_request", "call_video_response", "call_status",
}

# ─────────────────────── Presence (cache) ───────────────────────
PRESENCE_TTL = 90          # client pings every 25s → refreshed long before this


def k_online(uid): return f"rt:online:{uid}"
def k_seen(uid): return f"rt:last_seen:{uid}"


def _presence_connect(uid):
    """+1 connection. Returns True if the user just came online."""
    key = k_online(uid)
    cache.add(key, 0, PRESENCE_TTL)
    try:
        count = cache.incr(key)
    except ValueError:            # expired between add and incr
        cache.set(key, 1, PRESENCE_TTL)
        count = 1
    cache.touch(key, PRESENCE_TTL)
    return count == 1


def _presence_disconnect(uid):
    """-1 connection. Returns (went_offline, last_seen_iso)."""
    key = k_online(uid)
    try:
        count = cache.decr(key)
    except ValueError:
        count = 0
    if count <= 0:
        cache.delete(key)
        now = timezone.now().isoformat()
        cache.set(k_seen(uid), now, None)
        return True, now
    return False, None


def _heartbeat(uid):
    if not cache.touch(k_online(uid), PRESENCE_TTL):
        cache.set(k_online(uid), 1, PRESENCE_TTL)


def _presence_of(uids):
    keys = [k_online(u) for u in uids] + [k_seen(u) for u in uids]
    got = cache.get_many(keys)
    return [
        {
            "user_id": u,
            "online": bool(got.get(k_online(u))),
            "last_seen": got.get(k_seen(u)),
        }
        for u in uids
    ]


class RealtimeConsumer(AsyncWebsocketConsumer):

    # ═════════════════════════ CONNECT / DISCONNECT ═════════════════════════
    async def connect(self):
        self.user = self.scope["user"]
        if self.user.is_anonymous:
            await self.close()
            return

        self.uid = str(self.user.id)
        self.req_id = None
        self.joined_groups = [
            user_group(self.uid),
            f"user_chat_{self.uid}",   # old group names → existing views still work
            f"user_call_{self.uid}",
        ]
        for g in self.joined_groups:
            await self.channel_layer.group_add(g, self.channel_name)

        await self.accept()
        await self.send_json({
            "event": "connected",
            "type": "connected",
            "payload": {
                "user_id": self.uid,
                "server_time": timezone.now().isoformat(),
            },
        })

        # ── presence ──
        came_online = await sync_to_async(_presence_connect)(self.uid)
        self.contact_ids = await self.get_contact_ids()
        if came_online:
            await self.broadcast_presence(True, None)
        await self.send_json(self.frame("presence_state", {
            "users": await sync_to_async(_presence_of)(self.contact_ids),
        }))

        # ── app opened from a call push / restarted mid-call → ring / resume ──
        for event, data in await self.pending_calls():
            await self.send_json(self.frame(event, data, with_request=False))

    async def disconnect(self, close_code):
        for g in getattr(self, "joined_groups", []):
            await self.channel_layer.group_discard(g, self.channel_name)
        if not getattr(self, "uid", None):
            return
        went_offline, last_seen = await sync_to_async(_presence_disconnect)(self.uid)
        if went_offline:
            await self.save_last_seen()
            await self.broadcast_presence(False, last_seen)
        # NOTE: we do NOT end calls here. Media runs over LiveKit; a short socket
        # drop must not kill the call. The LiveKit webhook settles real drop-outs.

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
        self.req_id = data.get("request_id") or data.get("requestId")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = data  # allow flat messages too
        if not conversation_id:
            conversation_id = payload.get("conversation_id") or payload.get("conversationId")

        if event == "ping":
            await sync_to_async(_heartbeat)(self.uid)
            await self.send_json({"event": "pong", "type": "pong", "action": "pong"})
            return
        if event == "pong":
            return

        if event == "presence_query":
            ids = [str(u) for u in (payload.get("user_ids") or [])][:500]
            await self.send_json(self.frame("presence_state", {
                "users": await sync_to_async(_presence_of)(ids or self.contact_ids),
            }))
            return

        if event in LK_CALL_EVENTS:
            handler = getattr(self, event)
            await handler(payload, conversation_id)
            return

        if event in CALL_EVENTS:
            await self.handle_call(event, data, payload, conversation_id)
            return

        handlers = {
            "send_message": self.handle_send_message,
            "typing": self.handle_typing,
            "read_message": self.handle_read,
            "delivered_message": self.handle_delivered,
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
        text = (p.get("text") or "").strip()
        if not text and not p.get("reaction"):
            await self.send_error("Empty message", event="send_message")
            return
        client_id = p.get("client_id") or p.get("clientId")
        try:
            message = await self.save_message(conv_id, p)
        except PermissionError as exc:
            await self.send_error(str(exc), event="send_message")
            return
        except Exception as exc:
            await self.send_error("Unable to send message", event="send_message",
                                  details=str(exc))
            return
        message["client_id"] = client_id

        # 1. ack to the sending device → "sending" becomes "sent ✓"
        await self.send_json({
            "event": "message_ack",
            "type": "message_ack",
            "conversation_id": conv_id,
            "request_id": self.req_id,
            "data": {"client_id": client_id, "message": message},
        })

        # 2. everyone in the conversation (incl. my other devices, not this one)
        await self.fan_out(
            conv_id,
            self.envelope("new_message", conv_id, message),
            include_self=True,
            exclude_channel=self.channel_name,
        )

        # 3. FCM push for members whose app is closed
        await self.push_message(message["id"])

    async def handle_typing(self, conv_id, p):
        is_typing = str(p.get("is_typing", False)).lower() == "true"
        name = await self.get_user_display_name(self.uid)
        await self.fan_out(
            conv_id,
            self.envelope("typing", conv_id, {
                "user_id": self.uid,
                "name": name,
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
                "user_id": self.uid,
            }),
            include_self=True,                  # clears unread on my other devices
            exclude_channel=self.channel_name,
        )

    async def handle_delivered(self, conv_id, p):
        """Not stored – just tells the sender their message reached my phone (✓✓ grey)."""
        await self.fan_out(
            conv_id,
            self.envelope("message_delivered", conv_id, {
                "message_id": p.get("message_id"),
                "user_id": self.uid,
            }),
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

    # ─────────────────────────────── Presence ───────────────────────────────
    async def broadcast_presence(self, online, last_seen):
        data = self.frame("presence", {
            "user_id": self.uid,
            "online": online,
            "last_seen": last_seen,
        })
        for uid in self.contact_ids:
            await self.send_to_user(uid, data)

    # ═════════════════════════════ LIVEKIT CALLS ═════════════════════════════
    #
    # All logic lives in call_service.py (shared with the REST endpoints).
    #   caller  → call_start {conversation_id, is_video}
    #             ← call_started {call…, token, url}   or  call_busy
    #             others ← call_invite (+ FCM push)
    #   callee  → call_accept {call_id}   ← call_joined {token, url}
    #             others ← call_accepted ; my other devices ← call_answered_elsewhere
    #   callee  → call_decline {call_id}  (1:1 → everyone ← call_ended reason=declined)
    #   caller  → call_cancel {call_id, reason}
    #   anyone  → call_leave {call_id}    (1:1 / last person → call_ended)
    #   anyone  → call_add {call_id, user_ids}
    #   anyone  → call_video_request / call_video_response {call_id, accepted}
    #   anyone  → call_status {conversation_id}

    async def _call(self, fn, *args, **kwargs):
        """Run a call_service function and send its result (or error) back."""
        try:
            result = await database_sync_to_async(fn)(*args, **kwargs)
        except cs.CallError as exc:
            return await self.send_error(exc.message, http_status=exc.status, **exc.extra)
        if result:
            event = result.pop("event", None)
            if event:
                await self.send_json(self.frame(event, result))
        return result

    async def call_start(self, p, conversation_id):
        await self._call(
            cs.start_call, self.user, conversation_id or p.get("conversation_id"),
            is_video=_truthy(p.get("is_video", p.get("isVideoCall", False))),
            join_existing=True,
        )

    async def call_accept(self, p, _conv):
        await self._call(cs.join_call, self.user, _call_id(p),
                         exclude_channel=self.channel_name)

    async def call_decline(self, p, _conv):
        await self._call(cs.decline_call, self.user, _call_id(p),
                         reason="busy" if p.get("reason") == "busy" else "declined",
                         exclude_channel=self.channel_name)

    async def call_cancel(self, p, _conv):
        await self._call(cs.cancel_call, self.user, _call_id(p),
                         reason=p.get("reason") or "cancelled")

    async def call_leave(self, p, _conv):
        await self._call(cs.leave_call, self.user, _call_id(p))

    async def call_add(self, p, _conv):
        await self._call(cs.add_to_call, self.user, _call_id(p), p.get("user_ids") or [])

    async def call_video_request(self, p, _conv):
        await self._call(cs.relay_in_call, self.user, _call_id(p), "call_video_request")

    async def call_video_response(self, p, _conv):
        accepted = _truthy(p.get("accepted"))
        await self._call(cs.relay_in_call, self.user, _call_id(p), "call_video_response",
                         {"accepted": accepted})
        if accepted:
            await database_sync_to_async(cs.set_camera)(self.user, _call_id(p), True)

    async def call_status(self, p, conversation_id):
        await self._call(cs.call_status, self.user, conversation_id or p.get("conversation_id"))

    # ─────────────────────── Legacy WebRTC relay (unchanged) ───────────────────────
    async def handle_call(self, event, data, payload, conversation_id):
        me = self.uid

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

        await self.send_to_user(target, {
            "event": event, "type": event, "from_user": me,
            "target_user": target, "conversation_id": conv, "payload": payload,
        })

        if event == CALL_OFFER:
            is_video = _truthy(payload.get("is_video_call", payload.get("isVideoCall", False)))
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

        if event in CALL_STOP_EVENTS:
            await self.send_to_user(target, {
                "event": "call_cancelled", "type": "call_cancelled",
                "payload": {
                    "conversation_id": conv, "call_id": call_id, "callId": call_id,
                    "from": me, "from_user": me, "reason": event,
                },
            })

        if event == CALL_ANSWER:
            await self.send_to_user(me, {
                "event": "call_answered_elsewhere", "type": "call_answered_elsewhere",
                "payload": {"call_id": call_id, "conversation_id": conv},
            }, exclude_channel=self.channel_name)

        await self.send_json({
            "event": "call_event_sent", "type": "call_event_sent",
            "payload": {
                "sent_event": event, "from_user": me, "target_user": target,
                "conversation_id": conv, "call_id": call_id, "callId": call_id,
            },
        })

    # ═════════════════════════════ ROUTING HELPERS ═════════════════════════════
    @staticmethod
    def envelope(event, conv_id, data):
        return {"event": event, "type": event, "conversation_id": conv_id,
                "data": data, "payload": data}

    def frame(self, event, data, with_request=True):
        f = {"event": event, "type": event, "data": data, "payload": data,
             "conversation_id": data.get("conversation_id")}
        if with_request and self.req_id:
            f["request_id"] = self.req_id
        return f

    async def send_to_user(self, user_id, data, exclude_channel=None):
        await self.channel_layer.group_send(
            user_group(user_id),
            {"type": "deliver", "data": data, "exclude_channel": exclude_channel},
        )

    async def fan_out(self, conv_id, data, include_self=False, exclude_channel=None):
        """Send `data` to every member of the conversation (each on all devices)."""
        for uid in await self.member_ids(conv_id):
            if uid == self.uid and not include_self:
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
        self.contact_ids = await self.get_contact_ids()

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
        frame = {"event": "error", "type": "error", "error": message, **extra}
        if self.req_id:
            frame["request_id"] = self.req_id
        await self.send_json(frame)

    # ═════════════════════════════════ DATABASE ═════════════════════════════════
    @database_sync_to_async
    def is_member(self, conversation_id):
        try:
            return ConversationMember.objects.filter(
                conversation_id=conversation_id, user_id=self.user.id
            ).exists()
        except (ValueError, TypeError):
            return False

    @database_sync_to_async
    def member_ids(self, conversation_id):
        return [
            str(uid) for uid in ConversationMember.objects
            .filter(conversation_id=conversation_id)
            .values_list("user_id", flat=True)
        ]

    @database_sync_to_async
    def get_contact_ids(self):
        """Everyone I share at least one conversation with."""
        conv_ids = ConversationMember.objects.filter(
            user_id=self.user.id
        ).values_list("conversation_id", flat=True)
        return [
            str(u) for u in ConversationMember.objects
            .filter(conversation_id__in=list(conv_ids))
            .exclude(user_id=self.user.id)
            .values_list("user_id", flat=True)
            .distinct()
        ]

    @database_sync_to_async
    def pending_calls(self):
        try:
            return cs.pending_for_user(self.user)
        except Exception:
            return []

    @database_sync_to_async
    def save_last_seen(self):
        try:
            User._meta.get_field("last_seen")
        except Exception:
            return  # model has no last_seen column → cache only
        User.objects.filter(id=self.user.id).update(last_seen=timezone.now())

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
        member = ConversationMember.objects.select_related("conversation").filter(
            conversation_id=conversation_id, user=self.user
        ).first()
        if not member:
            raise PermissionError("You are not a member")
        if member.is_blocked:
            raise PermissionError("You are blocked in this chat")

        message = Message.objects.create(
            conversation_id=conversation_id,
            sender=self.user,
            message_type=data.get("message_type") or Message.TEXT,
            text=(data.get("text") or "").strip(),
            reply_to_id=data.get("reply_to") or None,
            reaction_to_id=data.get("reaction_to") or None,
            reaction=data.get("reaction") or None,
        )
        message.read_by.add(self.user)
        member.conversation.save(update_fields=["updated_at"])  # bump in chat list

        # Same JSON the REST endpoints return (absolute URLs need
        # settings.PUBLIC_BASE_URL because there's no request here).
        try:
            return dict(serialize_message(message))
        except Exception:
            return {
                "id": message.id,
                "conversation": message.conversation_id,
                "conversation_id": message.conversation_id,
                "sender": {
                    "id": self.user.id,
                    "phone": getattr(self.user, "phone", ""),
                    "full_name": getattr(self.user, "full_name", "") or "",
                    "profile_picture": user_avatar_url(self.user) or None,
                },
                "message_type": message.message_type,
                "text": message.text,
                "reply_to": message.reply_to_id,
                "reaction_to": message.reaction_to_id,
                "reaction": message.reaction,
                "is_edited": message.is_edited,
                "is_deleted": message.is_deleted,
                "read_by": [self.user.id],
                "created_at": message.created_at.isoformat(),
            }

    @database_sync_to_async
    def push_message(self, message_id):
        try:
            message = Message.objects.select_related("conversation").get(id=message_id)
            push_new_message(message, self.user)
        except Exception:
            pass  # a push failure must never break realtime chat

    @database_sync_to_async
    def mark_read(self, conversation_id, message_id):
        try:
            message = Message.objects.get(id=message_id, conversation_id=conversation_id)
        except (Message.DoesNotExist, ValueError, TypeError):
            return False
        # Reading the newest message means everything before it is read too.
        older = Message.objects.filter(
            conversation_id=conversation_id, created_at__lte=message.created_at
        ).exclude(read_by=self.user).exclude(sender=self.user)
        for m in older[:500]:
            m.read_by.add(self.user)
        message.read_by.add(self.user)
        return True

    @database_sync_to_async
    def edit_message(self, conversation_id, message_id, text):
        try:
            message = Message.objects.get(
                id=message_id, sender=self.user, conversation_id=conversation_id
            )
        except (Message.DoesNotExist, ValueError, TypeError):
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
        except (Message.DoesNotExist, ValueError, TypeError):
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
        return user_display_name(user) if user else "Hiddenly user"

    @database_sync_to_async
    def get_user_avatar(self, user_id):
        user = User.objects.filter(id=user_id).first()
        return user_avatar_url(user) if user else ""


# ─────────────────────────── small utils ───────────────────────────
def _truthy(v):
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _call_id(p):
    return str(p.get("call_id") or p.get("callId") or "")