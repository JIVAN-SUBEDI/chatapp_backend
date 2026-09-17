import json
import mimetypes

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from .models import Conversation, ConversationMember, Message


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]
        self.conversation_id = self.scope["url_route"]["kwargs"]["conversation_id"]
        self.room_group_name = f"chat_{self.conversation_id}"

        if self.user.is_anonymous:
            await self.close()
            return

        is_member = await self.check_member()

        if not is_member:
            await self.close()
            return

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )

        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(
            self.room_group_name,
            self.channel_name
        )

    async def receive(self, text_data):
        data = json.loads(text_data)
        print(data)
        action = data.get("action", "send_message")

        if action == "send_message":
            message = await self.save_message(data)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "chat_message",
                    "message": message,
                }
            )

        elif action == "typing":
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "typing_event",
                    "user": {
                        "id": self.user.id,
                        "phone": self.user.phone,
                    },
                    "is_typing": data.get("is_typing", False),
                }
            )

        elif action == "read_message":
            message_id = data.get("message_id")
            await self.mark_read(message_id)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "read_event",
                    "message_id": message_id,
                    "user_id": self.user.id,
                }
            )

        elif action == "delete_message":
            message_id = data.get("message_id")
            result = await self.delete_message(message_id)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "delete_event",
                    "message": result,
                }
            )

        elif action == "edit_message":
            message_id = data.get("message_id")
            text = data.get("text", "")
            result = await self.edit_message(message_id, text)

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "edit_event",
                    "message": result,
                }
            )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            "action": "new_message",
            "message": event["message"],
        }))

    async def typing_event(self, event):
        await self.send(text_data=json.dumps({
            "action": "typing",
            "user": event["user"],
            "is_typing": event["is_typing"],
        }))

    async def read_event(self, event):
        await self.send(text_data=json.dumps({
            "action": "read_message",
            "message_id": event["message_id"],
            "user_id": event["user_id"],
        }))

    async def delete_event(self, event):
        await self.send(text_data=json.dumps({
            "action": "delete_message",
            "message": event["message"],
        }))

    async def edit_event(self, event):
        await self.send(text_data=json.dumps({
            "action": "edit_message",
            "message": event["message"],
        }))
    async def call_event(self, event):
        print("========== CHAT CALL EVENT ==========")
        print("event:", event)
        print("=====================================")

        data = (
            event.get("data")
            or event.get("message")
            or event.get("payload")
            or {}
        )

        await self.send(
            text_data=json.dumps({
                "action": "call_event",
                "event": "call_event",
                "type": "call_event",
                "data": data,
            })
        )
    @database_sync_to_async
    def check_member(self):
        return ConversationMember.objects.filter(
            conversation_id=self.conversation_id,
            user=self.user
        ).exists()

    @database_sync_to_async
    def save_message(self, data):
        message = Message.objects.create(
            conversation_id=self.conversation_id,
            sender=self.user,
            message_type=data.get("message_type", Message.TEXT),
            text=data.get("text", ""),
            reply_to_id=data.get("reply_to"),
            reaction_to_id=data.get("reaction_to"),
            reaction=data.get("reaction"),
        )

        message.read_by.add(self.user)

        conversation = Conversation.objects.get(id=self.conversation_id)
        conversation.save()

        return {
            "id": message.id,
            "conversation": message.conversation_id,
            "sender": {
                "id": self.user.id,
                "phone": self.user.phone,
            
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
    def mark_read(self, message_id):
        try:
            message = Message.objects.get(
                id=message_id,
                conversation_id=self.conversation_id
            )
            message.read_by.add(self.user)
            return True
        except Message.DoesNotExist:
            return False

    @database_sync_to_async
    def delete_message(self, message_id):
        try:
            message = Message.objects.get(
                id=message_id,
                sender=self.user,
                conversation_id=self.conversation_id
            )

            message.text = ""
            message.media = None
            message.thumbnail = None
            message.is_deleted = True
            message.save()

            return {
                "id": message.id,
                "is_deleted": True,
            }

        except Message.DoesNotExist:
            return {
                "error": "Message not found"
            }

    @database_sync_to_async
    def edit_message(self, message_id, text):
        try:
            message = Message.objects.get(
                id=message_id,
                sender=self.user,
                conversation_id=self.conversation_id
            )

            message.text = text
            message.is_edited = True
            message.save()

            return {
                "id": message.id,
                "text": message.text,
                "is_edited": True,
            }

        except Message.DoesNotExist:
            return {
                "error": "Message not found"
            }