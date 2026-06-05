from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth import get_user_model

from rest_framework_simplejwt.tokens import AccessToken
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError


User = get_user_model()


@database_sync_to_async
def get_user_from_token(token):
    if not token:
        return AnonymousUser()

    try:
        access_token = AccessToken(token)
        payload = access_token.payload

        user_id = (
            payload.get("user_id")
            or payload.get("id")
            or payload.get("sub")
        )

        if not user_id:
            return AnonymousUser()

        return User.objects.get(id=user_id)

    except (InvalidToken, TokenError, User.DoesNotExist, KeyError):
        return AnonymousUser()


class JWTAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode()
        query_params = parse_qs(query_string)

        token = None

        if "token" in query_params:
            token = query_params["token"][0]

        scope["user"] = await get_user_from_token(token)

        return await self.app(scope, receive, send)


def JWTAuthMiddlewareStack(app):
    return JWTAuthMiddleware(app)