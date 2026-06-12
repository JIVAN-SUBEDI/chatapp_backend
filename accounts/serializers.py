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
MAX_OTP_ATTEMPTS = 5

User = get_user_model()

E164_REGEX = re.compile(r"^\+[1-9]\d{7,14}$")

LOCAL_OTP_PREFIX = "local:"
TWILIO_VERIFY_PREFIX = "twilio_verify:"


def normalize_phone(phone: str) -> str:
    return str(phone or "").strip().replace(" ", "").replace("-", "")


def normalize_e164(phone: str) -> str:
    phone = normalize_phone(phone)

    if not E164_REGEX.match(phone):
        raise serializers.ValidationError(
            "Phone must include country code in E.164 format, e.g. +9779800000000"
        )

    return phone


def twilio_verify_enabled() -> bool:
    """
    True = production Twilio Verify SMS.
    False = local/dev mode. OTP is printed in the console.
    """
    return bool(getattr(settings, "TWILIO_ENABLE_SMS", False))


def get_twilio_verify_client():
    account_sid = getattr(settings, "TWILIO_ACCOUNT_SID", "")
    auth_token = getattr(settings, "TWILIO_AUTH_TOKEN", "")
    service_sid = getattr(settings, "TWILIO_VERIFY_SERVICE_SID", "")

    if not account_sid or not auth_token or not service_sid:
        raise serializers.ValidationError({
            "phone": "Twilio Verify service is not configured properly."
        })

    return Client(account_sid, auth_token), service_sid


def start_otp_verification(*, phone: str, purpose: str = "login") -> str:

    phone = normalize_e164(phone)

    if not twilio_verify_enabled():
        code = f"{random.randint(100000, 999999)}"
        print(f"DEV OTP [{purpose}]: {phone} -> {code}")
        return LOCAL_OTP_PREFIX + make_password(code)

    try:
        client, service_sid = get_twilio_verify_client()

        print("========== TWILIO VERIFY DEBUG ==========")
        print("purpose:", purpose)
        print("to:", phone)
        print("service sid exists:", bool(service_sid))
        print("verify enabled:", twilio_verify_enabled())
        print("=========================================")

        verification = client.verify.v2.services(
            service_sid
        ).verifications.create(
            to=phone,
            channel="sms",
        )

        print("TWILIO VERIFY STARTED:", phone, verification.sid, verification.status)

        return TWILIO_VERIFY_PREFIX + verification.sid

    except TwilioRestException as e:
        print("========== TWILIO VERIFY SEND ERROR ==========")
        print("status:", getattr(e, "status", None))
        print("code:", getattr(e, "code", None))
        print("message:", getattr(e, "msg", str(e)))
        print("details:", getattr(e, "details", None))
        print("uri:", getattr(e, "uri", None))
        print("==============================================")

        raise serializers.ValidationError({
            "phone": f"Failed to send OTP: {getattr(e, 'msg', str(e))}"
        })

    except serializers.ValidationError:
        raise

    except Exception as e:
        print("========== OTP VERIFY START ERROR ==========")
        print(e)
        print("============================================")

        raise serializers.ValidationError({
            "phone": "Failed to send OTP. Please try again."
        })


def check_otp_verification(*, phone: str, code: str, otp_hash: str) -> bool:

    phone = normalize_e164(phone)
    code = str(code or "").strip()
    otp_hash = otp_hash or ""

    if otp_hash.startswith(TWILIO_VERIFY_PREFIX):
        if not twilio_verify_enabled():
            return False

        try:
            client, service_sid = get_twilio_verify_client()

            check = client.verify.v2.services(
                service_sid
            ).verification_checks.create(
                to=phone,
                code=code,
            )

            print("TWILIO VERIFY CHECK:", phone, check.status)

            return check.status == "approved"

        except TwilioRestException as e:
            print("========== TWILIO VERIFY CHECK ERROR ==========")
            print("status:", getattr(e, "status", None))
            print("code:", getattr(e, "code", None))
            print("message:", getattr(e, "msg", str(e)))
            print("details:", getattr(e, "details", None))
            print("uri:", getattr(e, "uri", None))
            print("================================================")
            return False

        except Exception as e:
            print("========== OTP VERIFY CHECK ERROR ==========")
            print(e)
            print("============================================")
            return False

    if otp_hash.startswith(LOCAL_OTP_PREFIX):
        real_hash = otp_hash[len(LOCAL_OTP_PREFIX):]
        return check_password(code, real_hash)

    # Backward compatibility for OTPs created by your old code.
    return check_password(code, otp_hash)


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

        now = timezone.now()

        # Resend cooldown
        if user.otp_expires_at and user.otp_hash and now < user.otp_expires_at:
            last_sent_at = user.otp_expires_at - timezone.timedelta(
                seconds=OTP_TTL_SECONDS,
            )

            seconds_since_last = int((now - last_sent_at).total_seconds())

            if seconds_since_last < RESEND_COOLDOWN_SECONDS:
                wait_seconds = RESEND_COOLDOWN_SECONDS - seconds_since_last

                raise serializers.ValidationError({
                    "phone": f"Please wait {wait_seconds}s before requesting a new OTP."
                })

        otp_hash = start_otp_verification(
            phone=phone,
            purpose="login",
        )

        user.otp_hash = otp_hash
        user.otp_expires_at = now + timezone.timedelta(seconds=OTP_TTL_SECONDS)
        user.otp_attempts = 0
        user.save(
            update_fields=[
                "otp_hash",
                "otp_expires_at",
                "otp_attempts",
            ]
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

        if user.otp_attempts >= MAX_OTP_ATTEMPTS:
            raise serializers.ValidationError({
                "code": "Too many attempts. Please request OTP again."
            })

        user.otp_attempts += 1
        user.save(update_fields=["otp_attempts"])

        is_valid = check_otp_verification(
            phone=phone,
            code=code,
            otp_hash=user.otp_hash,
        )

        if not is_valid:
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

        otp_hash = start_otp_verification(
            phone=new_phone,
            purpose="phone_change",
        )

        PhoneChangeOTP.objects.create(
            user=user,
            new_phone=new_phone,
            otp_hash=otp_hash,
            expires_at=now + timezone.timedelta(seconds=OTP_TTL_SECONDS),
            sent_at=now,
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
        ).order_by("-sent_at").first()

        if not otp:
            raise serializers.ValidationError({
                "code": "OTP not found. Please request again."
            })

        if now >= otp.expires_at:
            raise serializers.ValidationError({
                "code": "OTP expired. Please request again."
            })

        if otp.attempts >= MAX_OTP_ATTEMPTS:
            raise serializers.ValidationError({
                "code": "Too many attempts. Please request OTP again."
            })

        otp.attempts += 1
        otp.save(update_fields=["attempts"])

        is_valid = check_otp_verification(
            phone=new_phone,
            code=code,
            otp_hash=otp.otp_hash,
        )

        if not is_valid:
            raise serializers.ValidationError({
                "code": "Invalid OTP."
            })

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        user.phone = new_phone
        user.save(update_fields=["phone"])

        return user
