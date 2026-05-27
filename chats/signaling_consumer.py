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
CALL_JOIN = "call_join"
CALL_LEAVE = "call_leave"


class CallSignalingConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.user = self.scope["user"]

        if self.user.is_anonymous:
            await self.close()
            return

        is_member = await self.is_conversation_member(
            self.conversation_id,
            self.user.id
        )

        if not is_member:
            await self.close()
            return

        self.room_group_name = f"call_{self.conversation_id}"

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "call_signal",
                "event": CALL_JOIN,
                "from_user": self.user.id,
                "payload": {
                    "user_id": self.user.id,
                    "name": self.get_user_name(),
                },
            }
        )

    async def disconnect(self, close_code):
        if hasattr(self, "room_group_name"):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "call_signal",
                    "event": CALL_LEAVE,
                    "from_user": self.user.id,
                    "payload": {
                        "user_id": self.user.id,
                    },
                }
            )

            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )

    async def receive(self, text_data=None, bytes_data=None):
        try:
            data = json.loads(text_data or "{}")
        except json.JSONDecodeError:
            await self.send_json({
                "error": "Invalid JSON"
            })
            return

        event = data.get("event")
        payload = data.get("payload", {})
        target_user = data.get("target_user")

        allowed_events = [
            CALL_OFFER,
            CALL_ANSWER,
            ICE_CANDIDATE,
            CALL_REJECT,
            CALL_END,
            CALL_JOIN,
            CALL_LEAVE,
        ]

        if event not in allowed_events:
            await self.send_json({
                "error": "Invalid call event"
            })
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "call_signal",
                "event": event,
                "from_user": self.user.id,
                "target_user": target_user,
                "payload": payload,
            }
        )

    async def call_signal(self, event):
        if event.get("from_user") == self.user.id:
            return

        target_user = event.get("target_user")

        if target_user and int(target_user) != self.user.id:
            return

        await self.send_json({
            "event": event["event"],
            "from_user": event["from_user"],
            "target_user": target_user,
            "payload": event.get("payload", {}),
        })

    async def send_json(self, data):
        await self.send(text_data=json.dumps(data))

    def get_user_name(self):
        return (
            getattr(self.user, "full_name", None)
            or getattr(self.user, "username", "")
            or str(self.user.id)
        )

    @database_sync_to_async
    def is_conversation_member(self, conversation_id, user_id):
        return ConversationMember.objects.filter(
            conversation_id=conversation_id,
            user_id=user_id
        ).exists()