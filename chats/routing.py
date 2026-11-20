from django.urls import path,re_path
from .realtime_counsumers import RealtimeConsumer
from .consumers import ChatConsumer, GlobalChatConsumer
from .signaling_consumer import (
    CallSignalingConsumer,
    GlobalCallConsumer,
)

websocket_urlpatterns = [
    # Individual opened conversation
    path(
        "ws/chat/<int:conversation_id>/",
        ChatConsumer.as_asgi(),
    ),

    # Global CHAT socket
    # New group, chat-list updates, latest message, etc.
    path(
        "ws/global-chat/",
        GlobalChatConsumer.as_asgi(),
    ),

    # Existing global CALL socket
    path(
        "ws/global-call/",
        GlobalCallConsumer.as_asgi(),
    ),

    path(
        "ws/call/<int:conversation_id>/",
        CallSignalingConsumer.as_asgi(),
    ),
    re_path(r"ws/realtime/$", RealtimeConsumer.as_asgi()),
]