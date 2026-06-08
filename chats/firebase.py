import firebase_admin
from firebase_admin import credentials, messaging
from django.conf import settings


def init_firebase():
    if firebase_admin._apps:
        return

    cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_FILE)
    firebase_admin.initialize_app(cred)


def send_incoming_call_push(*, token, call_data):
    init_firebase()

    message = messaging.Message(
        token=token,
        data={
            "type": "incoming_call",
            "call_id": str(call_data["call_id"]),
            "conversation_id": str(call_data["conversation_id"]),
            "caller_id": str(call_data["caller_id"]),
            "caller_name": call_data.get("caller_name", ""),
            "caller_avatar": call_data.get("caller_avatar", ""),
            "is_video_call": "true" if call_data["is_video_call"] else "false",
        },
        android=messaging.AndroidConfig(
            priority="high",
        ),
    )

    return messaging.send(message)