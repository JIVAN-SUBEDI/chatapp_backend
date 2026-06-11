# firebase.py

import firebase_admin
from firebase_admin import credentials, messaging
from django.conf import settings
from firebase_admin import exceptions as firebase_exceptions
from datetime import timedelta
from  .models import UserFCMToken

def init_firebase():
    if firebase_admin._apps:
        return

    # Better for production:
    # FIREBASE_CREDENTIALS = BASE_DIR / "firebase_chat.json"
    cred = credentials.Certificate(settings.FIREBASE_CREDENTIALS)

    # cred = credentials.Certificate("./firebase_chat.json")
    firebase_admin.initialize_app(cred)


def delete_bad_fcm_token(token):
    if not token:
        return
    
    UserFCMToken.objects.filter(token=token).delete()    
    print("DELETE THIS INVALID FCM TOKEN FROM DB:", token)


def send_incoming_call_push(*, token, call_data):
    init_firebase()

    if not token:
        print("FCM incoming call skipped: receiver has no FCM token")
        return None

    data = {
        "type": "incoming_call",
        "call_id": str(call_data["call_id"]),
        "conversation_id": str(call_data["conversation_id"]),
        "caller_id": str(call_data["caller_id"]),
        "caller_name": str(call_data.get("caller_name", "")),
        "caller_avatar": str(call_data.get("caller_avatar", "")),
        "is_video_call": "true" if call_data.get("is_video_call") else "false",
    }

    message = messaging.Message(
        token=token,
        data=data,
        android=messaging.AndroidConfig(
            priority="high",
            ttl=timedelta(seconds=30),
        ),
    )

    try:
        response = messaging.send(message)
        print("FCM incoming call sent:", response)
        print("FCM incoming call token:", token)
        print("FCM incoming call data:", data)
        return response

    except messaging.UnregisteredError as e:
        print("FCM token is unregistered/expired:", str(e))
        print("Bad token:", token)
        delete_bad_fcm_token(token)
        return None

    except messaging.SenderIdMismatchError as e:
        print("FCM sender/project mismatch:", str(e))
        print("This token belongs to another Firebase project.")
        print("Check google-services.json and firebase_chat.json")
        print("Bad token:", token)
        delete_bad_fcm_token(token)
        return None

    except firebase_exceptions.NotFoundError as e:
        print("FCM token not found:", str(e))
        print("Bad token:", token)
        delete_bad_fcm_token(token)
        return None

    except Exception as e:
        print("FCM incoming call failed:", str(e))
        print("FCM incoming call token:", token)
        print("FCM incoming call data:", data)
        return None
def send_message_push(*, token, message_data):
    print("🔥 send_message_push CALLED")
    print("TOKEN:", token)
    print("MESSAGE DATA:", message_data)

    try:
        init_firebase()
        print("✅ Firebase initialized")

        if not token:
            print("❌ No FCM token found")
            return None

        title = str(message_data.get("title", "New message"))
        body = str(message_data.get("body", ""))

        print("TITLE:", title)
        print("BODY:", body)

        message = messaging.Message(
            token=token,
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data={
                "type": "new_message",
                "conversation_id": str(message_data["conversation_id"]),
                "message_id": str(message_data["message_id"]),
                "sender_id": str(message_data["sender_id"]),
                "sender_name": str(message_data.get("sender_name", "")),
                "sender_avatar": str(message_data.get("sender_avatar", "")),

                # IMPORTANT: do not use "message_type"
                "msg_type": str(message_data.get("message_type", "text")),

                "text": str(message_data.get("text", "")),
            },
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    channel_id="message_channel",
                    priority="high",
                    sound="default",
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                ),
            ),
        )

        print("📨 Sending FCM now...")
        response = messaging.send(message)
        print("✅ FCM message sent:", response)
        return response

    except Exception as e:
        print("❌ FCM send failed:", repr(e))
        return None