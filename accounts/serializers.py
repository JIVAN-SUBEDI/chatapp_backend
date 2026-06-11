import random
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password, check_password
from django.utils import timezone
from rest_framework import serializers
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from .models import PhoneChangeOTP

OTP_TTL_SECONDS = 300          # 5 minutes
RESEND_COOLDOWN_SECONDS = 60   # 60 seconds

User = get_user_model()

E164_REGEX = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(phone: str) -> str:
    return str(phone or "").strip().replace(" ", "").replace("-", "")


def normalize_e164(phone: str) -> str:
    phone = normalize_phone(phone)

    if not E164_REGEX.match(phone):
        raise serializers.ValidationError(
            "Phone must include country code in E.164 format, e.g. +9779800000000"
        )

    return phone


def send_otp_sms(*, phone: str, code: str, purpose: str = "login") -> None:
    """
    Sends OTP using Twilio.

    Required settings:
      TWILIO_ACCOUNT_SID
      TWILIO_AUTH_TOKEN
      TWILIO_PHONE_NUMBER
      TWILIO_ENABLE_SMS

    For local/dev:
      TWILIO_ENABLE_SMS=false
    """

    phone = normalize_e164(phone)

    sms_enabled = getattr(settings, "TWILIO_ENABLE_SMS", False)

    if not sms_enabled:
        print(f"DEV OTP [{purpose}]:", phone, code)
        return

    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    from_number = getattr(settings, "TWILIO_PHONE_NUMBER", "")

    print("========== TWILIO DEBUG ==========")
    print("purpose:", purpose)
    print("to:", phone)
    print("from:", from_number)
    print("sid exists:", bool(account_sid))
    print("token exists:", bool(auth_token))
    print("sms enabled:", sms_enabled)
    print("==================================")

    if not account_sid or not auth_token or not from_number:
        raise serializers.ValidationError({
            "phone": "SMS service is not configured properly."
        })

    if not from_number.startswith("+"):
        raise serializers.ValidationError({
            "phone": "Twilio sender number must be in E.164 format, e.g. +1234567890."
        })

    message_body = f"Your OTP code is {code}. It expires in 5 minutes."

    try:
        client = Client(account_sid, auth_token)

        message = client.messages.create(
            body=message_body,
            from_=from_number,
            to=phone,
        )

        print("TWILIO OTP SENT:", phone, message.sid)

    except TwilioRestException as e:
        print("========== TWILIO ERROR ==========")
        print("status:", getattr(e, "status", None))
        print("code:", getattr(e, "code", None))
        print("message:", getattr(e, "msg", str(e)))
        print("details:", getattr(e, "details", None))
        print("uri:", getattr(e, "uri", None))
        print("==================================")

        raise serializers.ValidationError({
            "phone": f"Failed to send OTP SMS: {getattr(e, 'msg', str(e))}"
        })

    except Exception as e:
        print("========== OTP SMS ERROR ==========")
        print(e)
        print("===================================")

        raise serializers.ValidationError({
            "phone": "Failed to send OTP SMS. Please try again."
        })


class SendOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()

    def validate_phone(self, value):
        return normalize_e164(value)

    def create(self, validated_data):
        phone = validated_data["phone"]

        user, _ = User.objects.get_or_create(
            phone=phone,
            defaults={"full_name": "Pending"},
        )

        # Resend cooldown
        if user.otp_expires_at and user.otp_hash and timezone.now() < user.otp_expires_at:
            last_sent_at = user.otp_expires_at - timezone.timedelta(
                seconds=OTP_TTL_SECONDS,
            )

            seconds_since_last = int(
                (timezone.now() - last_sent_at).total_seconds()
            )

            if seconds_since_last < RESEND_COOLDOWN_SECONDS:
                wait_seconds = RESEND_COOLDOWN_SECONDS - seconds_since_last

                raise serializers.ValidationError({
                    "phone": f"Please wait {wait_seconds}s before requesting a new OTP."
                })

        code = f"{random.randint(100000, 999999)}"
        otp_hash = make_password(code)
        expires_at = timezone.now() + timezone.timedelta(
            seconds=OTP_TTL_SECONDS,
        )

        # Save OTP before sending SMS
        user.otp_hash = otp_hash
        user.otp_expires_at = expires_at
        user.otp_attempts = 0
        user.save(
            update_fields=[
                "otp_hash",
                "otp_expires_at",
                "otp_attempts",
            ]
        )

        send_otp_sms(
            phone=phone,
            code=code,
            purpose="login",
        )

        return {
            "phone": phone,
            "expires_in": OTP_TTL_SECONDS,
            "resend_after": RESEND_COOLDOWN_SECONDS,
        }


class VerifyOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()
    code = serializers.CharField(min_length=4, max_length=8)

    def validate(self, attrs):
        # IMPORTANT:
        # SendOTP saves phone as E.164. Verify must also use E.164.
        attrs["phone"] = normalize_e164(attrs["phone"])
        attrs["code"] = attrs["code"].strip()
        return attrs

    def create(self, validated_data):
        phone = validated_data["phone"]
        code = validated_data["code"]

        user = User.objects.filter(phone=phone).first()

        if not user:
            raise serializers.ValidationError({
                "phone": "Phone not found. Request OTP again."
            })

        if not user.otp_hash or not user.otp_expires_at:
            raise serializers.ValidationError({
                "code": "OTP not requested. Please request OTP first."
            })

        if timezone.now() >= user.otp_expires_at:
            raise serializers.ValidationError({
                "code": "OTP expired. Please request again."
            })

        if user.otp_attempts >= 5:
            raise serializers.ValidationError({
                "code": "Too many attempts. Please request OTP again."
            })

        user.otp_attempts += 1
        user.save(update_fields=["otp_attempts"])

        if not check_password(code, user.otp_hash):
            raise serializers.ValidationError({
                "code": "Invalid OTP."
            })

        user.otp_hash = None
        user.otp_expires_at = None
        user.otp_attempts = 0
        user.save(
            update_fields=[
                "otp_hash",
                "otp_expires_at",
                "otp_attempts",
            ]
        )

        needs_profile = user.full_name == "Pending" or not user.full_name.strip()

        return {
            "user": user,
            "needs_profile": needs_profile,
        }


class CompleteSignupSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    bio = serializers.CharField(required=False, allow_blank=True)

    def create(self, validated_data):
        phone = self.context["phone"]
        full_name = validated_data["full_name"].strip()
        bio = validated_data.get("bio", "").strip()

        if not full_name:
            raise serializers.ValidationError({
                "full_name": "Full name is required."
            })

        user = User.objects.filter(phone=phone).first()

        if not user:
            raise serializers.ValidationError({
                "phone": "User not found. Verify OTP first."
            })

        user.full_name = full_name
        user.bio = bio
        user.save(update_fields=["full_name", "bio"])

        return user


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["full_name", "bio", "profile_picture"]

    def validate_full_name(self, value):
        value = value.strip()

        if not value:
            raise serializers.ValidationError("Full name cannot be empty.")

        return value


class RequestPhoneChangeOTPSerializer(serializers.Serializer):
    new_phone = serializers.CharField()

    def validate_new_phone(self, value):
        value = normalize_e164(value)
        user = self.context["request"].user

        if value == user.phone:
            raise serializers.ValidationError(
                "This is already your current phone number."
            )

        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError(
                "This phone number is already used by another account."
            )

        return value

    def create(self, validated_data):
        user = self.context["request"].user
        new_phone = validated_data["new_phone"]
        now = timezone.now()

        last = PhoneChangeOTP.objects.filter(
            user=user,
            new_phone=new_phone,
            is_used=False,
        ).order_by("-sent_at").first()

        if last:
            seconds_since = int((now - last.sent_at).total_seconds())

            if seconds_since < RESEND_COOLDOWN_SECONDS:
                wait_seconds = RESEND_COOLDOWN_SECONDS - seconds_since

                raise serializers.ValidationError({
                    "new_phone": f"Please wait {wait_seconds}s before requesting OTP again."
                })

        code = f"{random.randint(100000, 999999)}"

        PhoneChangeOTP.objects.create(
            user=user,
            new_phone=new_phone,
            otp_hash=make_password(code),
            expires_at=now + timezone.timedelta(seconds=OTP_TTL_SECONDS),
            sent_at=now,
        )

        send_otp_sms(
            phone=new_phone,
            code=code,
            purpose="phone_change",
        )

        return {
            "new_phone": new_phone,
            "expires_in": OTP_TTL_SECONDS,
            "resend_after": RESEND_COOLDOWN_SECONDS,
        }


class ConfirmPhoneChangeSerializer(serializers.Serializer):
    new_phone = serializers.CharField()
    code = serializers.CharField(min_length=4, max_length=8)

    def validate_new_phone(self, value):
        value = normalize_e164(value)
        user = self.context["request"].user

        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError(
                "This phone number is already used by another account."
            )

        return value

    def validate(self, attrs):
        attrs["new_phone"] = normalize_e164(attrs["new_phone"])
        attrs["code"] = attrs["code"].strip()
        return attrs

    def create(self, validated_data):
        user = self.context["request"].user
        new_phone = validated_data["new_phone"]
        code = validated_data["code"]
        now = timezone.now()

        otp = PhoneChangeOTP.objects.filter(
            user=user,
            new_phone=new_phone,
            is_used=False,
        ).order_by("-created_at").first()

        if not otp:
            raise serializers.ValidationError({
                "code": "OTP not found. Please request again."
            })

        if now >= otp.expires_at:
            raise serializers.ValidationError({
                "code": "OTP expired. Please request again."
            })

        if otp.attempts >= 5:
            raise serializers.ValidationError({
                "code": "Too many attempts. Please request OTP again."
            })

        otp.attempts += 1
        otp.save(update_fields=["attempts"])

        if not check_password(code, otp.otp_hash):
            raise serializers.ValidationError({
                "code": "Invalid OTP."
            })

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        user.phone = new_phone
        user.save(update_fields=["phone"])

        return user