import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")
django.setup()

from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from chats.routing import websocket_urlpatterns
from chats.middleware import JWTAuthMiddlewareStack

application = ProtocolTypeRouter({
    "http": get_asgi_application(),

    "websocket": JWTAuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})