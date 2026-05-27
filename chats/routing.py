from django.urls import path
from .consumers import ChatConsumer
from .signaling_consumer import CallSignalingConsumer

websocket_urlpatterns = [
    path("ws/chat/<int:conversation_id>/", ChatConsumer.as_asgi()),
    path("ws/call/<int:conversation_id>)/",CallSignalingConsumer.as_asgi()),
]