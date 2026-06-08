import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model

from .models import ConversationMember

User = get_user_model()

CALL_OFFER = "call_offer"
CALL_ANSWER = "call_answer"
ICE_CANDIDATE = "ice_candidate"

CALL_REJECT = "call_reject"
CALL_END = "call_end"
CALL_BUSY = "call_busy"
CALL_TIMEOUT = "call_timeout"

CALL_RENEGOTIATE_OFFER = "call_renegotiate_offer"
CALL_RENEGOTIATE_ANSWER = "call_renegotiate_answer"
CALL_VIDEO_TOGGLE = "call_video_toggle"
CALL_VIDEO_UPGRADE_REJECTED = "call_video_upgrade_rejected"


class CallSignalingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.user = self.scope["user"]

        if self.user.is_anonymous:
            await self.close()
            return

        is_member = await self.is_conversation_member(
            self.conversation_id,
            self.user.id,
        )

        if not is_member:
            await self.close()
            return

        self.room_group_name = f"call_{self.conversation_id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name,
        )

        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )

    async def receive(self, text_data=None, bytes_data=None):
        try:
            data = json.loads(text_data or "{}")
        except json.JSONDecodeError:
            await self.send_json({"error": "Invalid JSON"})
            return

        event = data.get("event")
        payload = data.get("payload") or {}
        target_user = data.get("target_user")
        conversation_id = data.get("conversation_id") or self.conversation_id

        allowed_events = {
            CALL_OFFER,
            CALL_ANSWER,
            ICE_CANDIDATE,
            CALL_REJECT,
            CALL_END,
            CALL_BUSY,
            CALL_TIMEOUT,
            CALL_RENEGOTIATE_OFFER,
            CALL_RENEGOTIATE_ANSWER,
            CALL_VIDEO_TOGGLE,
            CALL_VIDEO_UPGRADE_REJECTED,
        }

        if event not in allowed_events:
            await self.send_json({
                "error": "Invalid call event",
                "event": event,
            })
            return

        if not isinstance(payload, dict):
            payload = {}

        payload.setdefault("from", str(self.user.id))
        payload.setdefault("conversation_id", str(conversation_id))

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "call_signal",
                "event": event,
                "from_user": self.user.id,
                "target_user": str(target_user) if target_user is not None else None,
                "conversation_id": str(conversation_id),
                "payload": payload,
            },
        )

    async def call_signal(self, event):
        from_user = event.get("from_user")

        if str(from_user) == str(self.user.id):
            return

        target_user = event.get("target_user")

        if target_user is not None and str(target_user) != str(self.user.id):
            return

        await self.send_json({
            "event": event["event"],
            "from_user": from_user,
            "target_user": target_user,
            "conversation_id": event.get("conversation_id"),
            "payload": event.get("payload", {}),
        })

    async def send_json(self, data):
        await self.send(text_data=json.dumps(data))

    @database_sync_to_async
    def is_conversation_member(self, conversation_id, user_id):
        return ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id,
        ).exists()