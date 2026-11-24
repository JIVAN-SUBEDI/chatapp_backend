"""
LiveKit access tokens for Hiddenly calls (1:1 and group).

pip install livekit-api

settings.py
    LIVEKIT_URL = "wss://your-project.livekit.cloud"   # or your self-hosted wss URL
    LIVEKIT_API_KEY = "APIxxxxxxxx"
    LIVEKIT_API_SECRET = "xxxxxxxxxxxxxxxxxxxxxxxx"
"""
import json
from datetime import timedelta

from django.conf import settings
from livekit import api


def livekit_url():
    return settings.LIVEKIT_URL


def livekit_token(room, user_id, name="", avatar="", ttl_hours=3):
    """JWT that lets `user_id` join, publish and subscribe in `room` only."""
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    )
    return (
        api.AccessToken(settings.LIVEKIT_API_KEY, settings.LIVEKIT_API_SECRET)
        .with_identity(str(user_id))
        .with_name(name or str(user_id))
        .with_metadata(json.dumps({"avatar": avatar or ""}))
        .with_grants(grants)
        .with_ttl(timedelta(hours=ttl_hours))
        .to_jwt()
    )