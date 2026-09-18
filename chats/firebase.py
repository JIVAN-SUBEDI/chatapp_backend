# firebase.py

import os
from datetime import timedelta

import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin import exceptions as firebase_exceptions
from django.conf import settings

from .models import UserFCMToken


# ============================================================
# FIREBASE INITIALIZATION
# ============================================================

def init_firebase():
    """
    Initialize Firebase exactly once.

    Returns:
        firebase_admin.App
    """

    try:
        # If Firebase is already initialized, return existing app.
        return firebase_admin.get_app()

    except ValueError:
        # No Firebase app exists yet.
        pass

    firebase_credentials = getattr(
        settings,
        "FIREBASE_CREDENTIALS",
        None,
    )

    if not firebase_credentials:
        raise RuntimeError(
            "FIREBASE_CREDENTIALS is not configured in Django settings."
        )

    # Convert Path object -> string
    credential_path = str(firebase_credentials)

    print("========================================")
    print("🔥 INITIALIZING FIREBASE")
    print("Firebase credential path:", credential_path)
    print("Credential exists:", os.path.exists(credential_path))
    print("========================================")

    if not os.path.exists(credential_path):
        raise RuntimeError(
            f"Firebase credential file does not exist: "
            f"{credential_path}"
        )

    try:
        cred = credentials.Certificate(credential_path)

        app = firebase_admin.initialize_app(cred)

        print("✅ FIREBASE INITIALIZED SUCCESSFULLY")

        return app

    except Exception as e:
        print("❌ FIREBASE INITIALIZATION FAILED")
        print("Error type:", type(e).__name__)
        print("Error:", repr(e))
        raise


# ============================================================
# TOKEN HELPERS
# ============================================================

def _short_token(token):
    """Do not print complete FCM tokens in production logs."""

    if not token:
        return "EMPTY"

    token = str(token)

    if len(token) <= 20:
        return token

    return f"{token[:10]}...{token[-8:]}"


def delete_bad_fcm_token(token):
    if not token:
        return

    deleted, _ = UserFCMToken.objects.filter(
        token=token
    ).delete()

    print(
        "🗑️ Deleted invalid FCM token:",
        _short_token(token),
        "deleted rows:",
        deleted,
    )


# ============================================================
# INCOMING CALL PUSH
# ============================================================

def send_incoming_call_push(*, token, call_data):

    print("")
    print("========================================")
    print("📞 SEND INCOMING CALL PUSH CALLED")
    print("TOKEN:", _short_token(token))
    print("CALL ID:", call_data.get("call_id"))
    print("CONVERSATION:", call_data.get("conversation_id"))
    print("========================================")

    if not token:
        print("❌ Incoming call push skipped: no token")
        return None

    try:
        # IMPORTANT:
        # Firebase initialization must be inside try,
        # otherwise StartCallView hides initialization errors.
        init_firebase()

        print("✅ Firebase available")

        data = {
            "type": "incoming_call",

            "call_id": str(
                call_data.get("call_id", "")
            ),

            "call_uuid": str(
                call_data.get("call_uuid", "")
            ),

            "conversation_id": str(
                call_data.get("conversation_id", "")
            ),

            "caller_id": str(
                call_data.get("caller_id", "")
            ),

            "caller_name": str(
                call_data.get("caller_name", "")
            ),

            "caller_avatar": str(
                call_data.get("caller_avatar", "")
            ),

            "conversation_name": str(
                call_data.get("conversation_name", "")
            ),

            "call_type": str(
                call_data.get("call_type", "audio")
            ),

            "is_video_call": (
                "true"
                if call_data.get("is_video_call")
                else "false"
            ),
        }

        print("📦 FCM CALL DATA:", data)

        message = messaging.Message(
            token=str(token),

            # Data-only message is intentional for incoming calls.
            data=data,

            android=messaging.AndroidConfig(
                priority="high",

                # Call should not arrive minutes later.
                ttl=timedelta(seconds=45),
            ),
        )

        print("📨 Sending incoming call FCM...")

        response = messaging.send(message)

        print("✅ INCOMING CALL FCM SENT")
        print("FCM RESPONSE:", response)
        print("TOKEN:", _short_token(token))

        return response

    # --------------------------------------------------------
    # TOKEN NO LONGER EXISTS
    # --------------------------------------------------------

    except messaging.UnregisteredError as e:

        print("❌ FCM TOKEN UNREGISTERED")
        print("Error:", repr(e))
        print("Token:", _short_token(token))

        delete_bad_fcm_token(token)

        return None

    # --------------------------------------------------------
    # TOKEN FROM DIFFERENT FIREBASE PROJECT
    # --------------------------------------------------------

    except messaging.SenderIdMismatchError as e:

        print("❌ FCM SENDER ID / PROJECT MISMATCH")
        print("Error:", repr(e))
        print("Token:", _short_token(token))

        print(
            "Check that google-services.json and "
            "firebase service-account JSON belong "
            "to the SAME Firebase project."
        )

        delete_bad_fcm_token(token)

        return None

    # --------------------------------------------------------
    # INVALID FCM ARGUMENT / TOKEN
    # --------------------------------------------------------

    except firebase_exceptions.InvalidArgumentError as e:

        print("❌ INVALID FCM TOKEN / ARGUMENT")
        print("Error:", repr(e))
        print("Token:", _short_token(token))

        delete_bad_fcm_token(token)

        return None

    # --------------------------------------------------------
    # FIREBASE RESOURCE NOT FOUND
    # --------------------------------------------------------

    except firebase_exceptions.NotFoundError as e:

        print("❌ FCM RESOURCE NOT FOUND")
        print("Error:", repr(e))
        print("Token:", _short_token(token))

        delete_bad_fcm_token(token)

        return None

    # --------------------------------------------------------
    # EVERYTHING ELSE
    # --------------------------------------------------------

    except Exception as e:

        print("")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("❌ INCOMING CALL FCM FAILED")
        print("ERROR TYPE:", type(e).__name__)
        print("ERROR:", repr(e))
        print("TOKEN:", _short_token(token))
        print("CALL DATA:", call_data)
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("")

        # Do NOT automatically delete the token here.
        #
        # This might be:
        # - credential problem
        # - network problem
        # - Firebase outage
        # - server configuration problem
        #
        # Token itself may still be valid.

        return None


# ============================================================
# NORMAL CHAT MESSAGE PUSH
# ============================================================

def send_message_push(*, token, message_data):

    print("")
    print("========================================")
    print("💬 SEND MESSAGE PUSH CALLED")
    print("TOKEN:", _short_token(token))
    print("========================================")

    if not token:
        print("❌ No FCM token")
        return None

    try:
        init_firebase()

        print("✅ Firebase available")

        title = str(
            message_data.get(
                "title",
                "New message",
            )
        )

        body = str(
            message_data.get(
                "body",
                "",
            )
        )

        data = {
            "type": "new_message",

            "conversation_id": str(
                message_data.get(
                    "conversation_id",
                    "",
                )
            ),

            "message_id": str(
                message_data.get(
                    "message_id",
                    "",
                )
            ),

            "sender_id": str(
                message_data.get(
                    "sender_id",
                    "",
                )
            ),

            "sender_name": str(
                message_data.get(
                    "sender_name",
                    "",
                )
            ),

            "sender_avatar": str(
                message_data.get(
                    "sender_avatar",
                    "",
                )
            ),

            "msg_type": str(
                message_data.get(
                    "message_type",
                    "text",
                )
            ),

            "text": str(
                message_data.get(
                    "text",
                    "",
                )
            ),
        }

        message = messaging.Message(
            token=str(token),

            notification=messaging.Notification(
                title=title,
                body=body,
            ),

            data=data,

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

        print("📨 Sending message FCM...")

        response = messaging.send(message)

        print("✅ MESSAGE FCM SENT")
        print("FCM RESPONSE:", response)

        return response

    except messaging.UnregisteredError as e:

        print("❌ MESSAGE TOKEN UNREGISTERED")
        print("Error:", repr(e))

        delete_bad_fcm_token(token)

        return None

    except messaging.SenderIdMismatchError as e:

        print("❌ MESSAGE FCM PROJECT MISMATCH")
        print("Error:", repr(e))

        delete_bad_fcm_token(token)

        return None

    except firebase_exceptions.InvalidArgumentError as e:

        print("❌ INVALID MESSAGE FCM TOKEN")
        print("Error:", repr(e))

        delete_bad_fcm_token(token)

        return None

    except firebase_exceptions.NotFoundError as e:

        print("❌ MESSAGE FCM TOKEN NOT FOUND")
        print("Error:", repr(e))

        delete_bad_fcm_token(token)

        return None

    except Exception as e:

        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print("❌ MESSAGE FCM SEND FAILED")
        print("ERROR TYPE:", type(e).__name__)
        print("ERROR:", repr(e))
        print("TOKEN:", _short_token(token))
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")

        return None