import json
import uuid

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model

from .models import ConversationMember

User = get_user_model()

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


class GlobalCallConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]

        print("========== GLOBAL CALL CONNECT ==========")
        print("user:", self.user)
        print("is_anonymous:", self.user.is_anonymous)
        print("=========================================")

        if self.user.is_anonymous:
            print("GLOBAL CALL CONNECT REJECTED: anonymous user")
            await self.close()
            return

        self.group_name = f"user_call_{self.user.id}"

        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name,
        )

        await self.accept()

        print("GLOBAL CALL CONNECTED")
        print("user_id:", self.user.id)
        print("group_name:", self.group_name)

        await self.send_json({
            "event": "global_call_connected",
            "type": "global_call_connected",
            "user_id": str(self.user.id),
            "payload": {
                "user_id": str(self.user.id),
            },
        })

    async def disconnect(self, close_code):
        print("========== GLOBAL CALL DISCONNECT ==========")
        print("user:", getattr(self, "user", None))
        print("close_code:", close_code)
        print("===========================================")

        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )

    async def incoming_call(self, event):
        print("========== GLOBAL INCOMING CALL EVENT ==========")
        print("event:", event)
        print("===============================================")

        await self.send_json(event.get("data", {}))

    async def call_cancelled(self, event):
        print("========== GLOBAL CALL CANCELLED EVENT ==========")
        print("event:", event)
        print("================================================")

        await self.send_json(event.get("data", {}))

    async def send_json(self, data):
        print("GLOBAL SEND JSON:", data)
        await self.send(text_data=json.dumps(data))


class CallSignalingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.user = self.scope["user"]

        print("")
        print("################################################")
        print("### CALL SOCKET CONNECT ATTEMPT")
        print("################################################")
        print("conversation_id:", self.conversation_id)
        print("user:", self.user)
        print("user_id:", getattr(self.user, "id", None))
        print("is_anonymous:", self.user.is_anonymous)
        print("################################################")

        if self.user.is_anonymous:
            print("CALL SOCKET CONNECT REJECTED: anonymous user")
            await self.close()
            return

        is_member = await self.is_conversation_member(
            self.conversation_id,
            self.user.id,
        )

        print("is_conversation_member:", is_member)

        if not is_member:
            print("CALL SOCKET CONNECT REJECTED: user not conversation member")
            await self.close()
            return

        self.room_group_name = f"call_{self.conversation_id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        await self.accept()

        print("CALL SOCKET CONNECTED SUCCESSFULLY")
        print("room_group_name:", self.room_group_name)
        print("user_id:", self.user.id)

        await self.send_json({
            "event": "call_socket_connected",
            "type": "call_socket_connected",
            "conversation_id": str(self.conversation_id),
            "user_id": str(self.user.id),
            "payload": {
                "conversation_id": str(self.conversation_id),
                "user_id": str(self.user.id),
            },
        })

    async def disconnect(self, close_code):
        print("")
        print("################################################")
        print("### CALL SOCKET DISCONNECT")
        print("################################################")
        print("conversation_id:", getattr(self, "conversation_id", None))
        print("user_id:", getattr(getattr(self, "user", None), "id", None))
        print("close_code:", close_code)
        print("################################################")

        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )

    async def receive(self, text_data=None, bytes_data=None):
        print("")
        print("################################################")
        print("### CALL SOCKET RECEIVE RAW")
        print("################################################")
        print("user_id:", getattr(self.user, "id", None))
        print("conversation_id:", getattr(self, "conversation_id", None))
        print("text_data:", text_data)
        print("bytes_data:", bytes_data)
        print("################################################")

        try:
            data = json.loads(text_data or "{}")
        except json.JSONDecodeError:
            print("CALL SOCKET ERROR: Invalid JSON")
            await self.send_json({"error": "Invalid JSON"})
            return

        event = data.get("event") or data.get("type")

        target_user = (
            data.get("target_user")
            or data.get("targetUser")
            or data.get("target_user_id")
            or data.get("targetUserId")
        )

        conversation_id = (
            data.get("conversation_id")
            or data.get("conversationId")
            or self.conversation_id
        )

        payload = data.get("payload")

        if not isinstance(payload, dict):
            payload = {
                key: value
                for key, value in data.items()
                if key not in {
                    "event",
                    "type",
                    "target_user",
                    "targetUser",
                    "target_user_id",
                    "targetUserId",
                    "conversation_id",
                    "conversationId",
                }
            }

        allowed_events = {
            CALL_OFFER,
            CALL_ANSWER,
            ICE_CANDIDATE,
            CALL_READY,
            CALL_REJECT,
            CALL_END,
            CALL_BUSY,
            CALL_TIMEOUT,
            CALL_RENEGOTIATE_OFFER,
            CALL_RENEGOTIATE_ANSWER,
            CALL_VIDEO_TOGGLE,
            CALL_VIDEO_UPGRADE_REJECTED,
        }

        print("========== CALL SOCKET PARSED ==========")
        print("event:", event)
        print("target_user:", target_user)
        print("conversation_id:", conversation_id)
        print("payload:", payload)
        print("allowed:", event in allowed_events)
        print("========================================")

        if event not in allowed_events:
            print("CALL SOCKET ERROR: Invalid call event:", event)
            await self.send_json({
                "error": "Invalid call event",
                "event": event,
                "received": data,
            })
            return

        if not isinstance(payload, dict):
            payload = {}

        if target_user is None or str(target_user).strip() == "":
            target_user = await self.get_other_member_user_id(
                conversation_id,
                self.user.id,
            )

        call_id = (
            payload.get("call_id")
            or payload.get("callId")
            or data.get("call_id")
            or data.get("callId")
        )

        if not call_id:
            call_id = str(uuid.uuid4())

        payload.setdefault("from", str(self.user.id))
        payload.setdefault("from_user", str(self.user.id))
        payload.setdefault("caller_id", str(self.user.id))
        payload.setdefault("callerId", str(self.user.id))

        payload.setdefault("conversation_id", str(conversation_id))
        payload.setdefault("conversationId", str(conversation_id))

        payload.setdefault("call_id", str(call_id))
        payload.setdefault("callId", str(call_id))

        if target_user is not None:
            payload.setdefault("target_user", str(target_user))
            payload.setdefault("targetUser", str(target_user))

        if event == CALL_OFFER:
            await self.send_incoming_call_to_global_socket(
                target_user=target_user,
                conversation_id=conversation_id,
                call_id=call_id,
                payload=payload,
            )

        if event in {CALL_REJECT, CALL_END, CALL_TIMEOUT}:
            await self.send_call_cancelled_to_global_socket(
                target_user=target_user,
                conversation_id=conversation_id,
                call_id=call_id,
                payload=payload,
                reason=event,
            )

        if event == CALL_READY:
            print("")
            print("################################################")
            print("### BACKEND RECEIVED CALL_READY")
            print("################################################")
            print("from_user:", self.user.id)
            print("target_user:", target_user)
            print("conversation_id:", conversation_id)
            print("payload:", payload)
            print("################################################")

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "call_signal",
                "event": event,
                "from_user": str(self.user.id),
                "target_user": str(target_user) if target_user is not None else None,
                "conversation_id": str(conversation_id),
                "payload": payload,
            },
        )

        await self.send_json({
            "event": "call_event_sent",
            "type": "call_event_sent",
            "sent_event": event,
            "from_user": str(self.user.id),
            "target_user": str(target_user) if target_user is not None else None,
            "conversation_id": str(conversation_id),
            "payload": {
                "sent_event": event,
                "from_user": str(self.user.id),
                "target_user": str(target_user) if target_user is not None else None,
                "conversation_id": str(conversation_id),
                "call_id": str(call_id),
                "callId": str(call_id),
            },
        })

    async def send_incoming_call_to_global_socket(
        self,
        target_user,
        conversation_id,
        call_id,
        payload,
    ):
        if target_user is None or str(target_user).strip() == "":
            print("GLOBAL INCOMING NOT SENT: target_user empty")
            return

        is_video_call = (
            payload.get("is_video_call") is True
            or payload.get("isVideoCall") is True
            or str(payload.get("is_video_call", "")).lower() == "true"
            or str(payload.get("isVideoCall", "")).lower() == "true"
        )

        caller_name = await self.get_user_display_name(self.user.id)
        caller_avatar = await self.get_user_avatar(self.user.id)

        global_payload = {
            "conversation_id": str(conversation_id),
            "conversationId": str(conversation_id),

            "caller_id": str(self.user.id),
            "callerId": str(self.user.id),

            "from": str(self.user.id),
            "from_user": str(self.user.id),

            "target_user": str(target_user),
            "targetUser": str(target_user),

            "caller_name": caller_name,
            "callerName": caller_name,

            "caller_avatar": caller_avatar,
            "callerAvatar": caller_avatar,

            "is_video_call": is_video_call,
            "isVideoCall": is_video_call,

            "call_id": str(call_id),
            "callId": str(call_id),

            "offer": payload.get("offer"),
        }

        print("")
        print("################################################")
        print("### SENDING GLOBAL incoming_call")
        print("################################################")
        print("to group:", f"user_call_{target_user}")
        print("payload:", global_payload)
        print("################################################")

        await self.channel_layer.group_send(
            f"user_call_{target_user}",
            {
                "type": "incoming_call",
                "data": {
                    "event": "incoming_call",
                    "type": "incoming_call",
                    "payload": global_payload,
                },
            },
        )

    async def send_call_cancelled_to_global_socket(
        self,
        target_user,
        conversation_id,
        call_id,
        payload,
        reason,
    ):
        if target_user is None or str(target_user).strip() == "":
            print("GLOBAL CANCEL NOT SENT: target_user empty")
            return

        global_payload = {
            "conversation_id": str(conversation_id),
            "conversationId": str(conversation_id),

            "from": str(self.user.id),
            "from_user": str(self.user.id),

            "caller_id": str(self.user.id),
            "callerId": str(self.user.id),

            "target_user": str(target_user),
            "targetUser": str(target_user),

            "call_id": str(call_id),
            "callId": str(call_id),

            "reason": reason,
        }

        print("")
        print("################################################")
        print("### SENDING GLOBAL call_cancelled")
        print("################################################")
        print("to group:", f"user_call_{target_user}")
        print("payload:", global_payload)
        print("################################################")

        await self.channel_layer.group_send(
            f"user_call_{target_user}",
            {
                "type": "call_cancelled",
                "data": {
                    "event": "call_cancelled",
                    "type": "call_cancelled",
                    "payload": global_payload,
                },
            },
        )

    async def call_signal(self, event):
        from_user = event.get("from_user")
        target_user = event.get("target_user")
        signal_event = event.get("event")
        payload = event.get("payload", {})

        print("")
        print("################################################")
        print("### CALL SIGNAL DELIVERY CHECK")
        print("################################################")
        print("current_socket_user:", self.user.id)
        print("signal_event:", signal_event)
        print("from_user:", from_user)
        print("target_user:", target_user)
        print("conversation_id:", event.get("conversation_id"))
        print("payload:", payload)
        print("################################################")

        if str(from_user) == str(self.user.id):
            print("CALL SIGNAL SKIPPED: sender socket")
            return

        if target_user is not None and str(target_user) != str(self.user.id):
            print("CALL SIGNAL SKIPPED: not target user")
            print("target_user:", target_user)
            print("current_user:", self.user.id)
            return

        response = {
            "event": signal_event,
            "type": signal_event,
            "from_user": from_user,
            "target_user": target_user,
            "conversation_id": event.get("conversation_id"),
            "payload": payload,
        }

        print("CALL SIGNAL DELIVERED:", response)

        await self.send_json(response)

    async def send_json(self, data):
        print("CALL SOCKET SEND JSON:", data)
        await self.send(text_data=json.dumps(data))

    @database_sync_to_async
    def is_conversation_member(self, conversation_id, user_id):
        return ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id,
        ).exists()

    @database_sync_to_async
    def get_other_member_user_id(self, conversation_id, current_user_id):
        member = (
            ConversationMember.objects
            .filter(conversation_id=conversation_id)
            .exclude(user_id=current_user_id)
            .first()
        )

        if member:
            return str(member.user_id)

        return None

    @database_sync_to_async
    def get_user_display_name(self, user_id):
        try:
            user = User.objects.get(id=user_id)

            for field in ["full_name", "name", "username", "phone"]:
                value = getattr(user, field, None)

                if value:
                    return str(value)

            return "Incoming call"
        except User.DoesNotExist:
            return "Incoming call"

    @database_sync_to_async
    def get_user_avatar(self, user_id):
        try:
            user = User.objects.get(id=user_id)

            image = (
                getattr(user, "profile_picture", None)
                or getattr(user, "avatar", None)
                or getattr(user, "image", None)
            )

            if image:
                try:
                    return image.url
                except Exception:
                    return str(image)

            return ""
        except User.DoesNotExist:
            return ""