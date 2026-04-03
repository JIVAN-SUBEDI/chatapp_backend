import random,re
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password, check_password
from rest_framework import serializers
from .models import PhoneChangeOTP
OTP_TTL_SECONDS = 300          # 5 minutes
RESEND_COOLDOWN_SECONDS = 60   # 60 seconds

User = get_user_model()
E164_REGEX = re.compile(r"^\+[1-9]\d{7,14}$")  # + and 8-15 digits total
def normalize_phone(phone: str) -> str:
    return phone.strip().replace(" ", "")

def normalize_e164(phone: str) -> str:
    phone = normalize_phone(phone)
    if not E164_REGEX.match(phone):
        raise serializers.ValidationError(
            "Phone must include country code in E.164 format, e.g. +9779800000000"
        )
    return phone


class SendOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()

    def validate_phone(self, value):
        return normalize_e164(value)

    def create(self, validated_data):
        phone = validated_data["phone"]
        user, _ = User.objects.get_or_create(
            phone=phone,
            defaults={"full_name": "Pending"}  # temporary placeholder
        )
        # resend cooldown
        if user.otp_expires_at and user.otp_hash and timezone.now() < user.otp_expires_at:
            last_sent_at = user.otp_expires_at - timezone.timedelta(seconds=OTP_TTL_SECONDS)
            print(user.otp_expires_at)
            print(last_sent_at)
            seconds_since_last = int((timezone.now() - last_sent_at).total_seconds())
            print(seconds_since_last)

            if seconds_since_last < RESEND_COOLDOWN_SECONDS:
                wait_seconds = RESEND_COOLDOWN_SECONDS - seconds_since_last
                raise serializers.ValidationError({
                    "phone": f"Please wait {wait_seconds}s before requesting a new OTP."
                })

        # Create OTP
        code = f"{random.randint(100000, 999999)}"
        otp_hash = make_password(code)
        expires_at = timezone.now() + timezone.timedelta(seconds=OTP_TTL_SECONDS)

        # Create user placeholder if not exists (without full_name yet)
        # IMPORTANT: We are NOT using create_user() here because it requires full_name
    

        user.otp_hash = otp_hash
        user.otp_expires_at = expires_at
        user.otp_attempts = 0
        user.save(update_fields=["otp_hash", "otp_expires_at", "otp_attempts"])

        # send SMS using provider (Twilio/Vonage/etc.)
        print("DEV OTP:", phone, code)

        return {"phone": phone, "expires_in": OTP_TTL_SECONDS}


class VerifyOTPSerializer(serializers.Serializer):
    phone = serializers.CharField()
    code = serializers.CharField(min_length=4, max_length=8)

    def validate(self, attrs):
        attrs["phone"] = normalize_phone(attrs["phone"])
        attrs["code"] = attrs["code"].strip()
        return attrs

    def create(self, validated_data):
        phone = validated_data["phone"]
        code = validated_data["code"]

        user = User.objects.filter(phone=phone).first()
        if not user:
            raise serializers.ValidationError({"phone": "Phone not found. Request OTP again."})

        if not user.otp_hash or not user.otp_expires_at:
            raise serializers.ValidationError({"code": "OTP not requested. Please request OTP first."})

        if timezone.now() >= user.otp_expires_at:
            raise serializers.ValidationError({"code": "OTP expired. Please request again."})

        if user.otp_attempts >= 5:
            raise serializers.ValidationError({"code": "Too many attempts. Please request OTP again."})

        # increment attempts
        user.otp_attempts += 1
        user.save(update_fields=["otp_attempts"])

        if not check_password(code, user.otp_hash):
            raise serializers.ValidationError({"code": "Invalid OTP."})

        # OTP verified: clear OTP fields (optional but recommended)
        user.otp_hash = None
        user.otp_expires_at = None
        user.otp_attempts = 0
        user.save(update_fields=["otp_hash", "otp_expires_at", "otp_attempts"])

        needs_profile = (user.full_name == "Pending" or not user.full_name.strip())
        return {"user": user, "needs_profile": needs_profile}


class CompleteSignupSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    bio = serializers.CharField(required=False, allow_blank=True)

    def create(self, validated_data):
        phone = self.context["phone"]
        full_name = validated_data["full_name"].strip()
        bio = validated_data.get("bio", "").strip()

        if not full_name:
            raise serializers.ValidationError({"full_name": "Full name is required."})

        user = User.objects.filter(phone=phone).first()
        if not user:
            raise serializers.ValidationError({"phone": "User not found. Verify OTP first."})

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
            raise serializers.ValidationError("This is already your current phone number.")

        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("This phone number is already used by another account.")

        return value

    def create(self, validated_data):
        user = self.context["request"].user
        new_phone = validated_data["new_phone"]
        now = timezone.now()

        # Cooldown: check last sent for this user + new_phone
        last = PhoneChangeOTP.objects.filter(user=user, new_phone=new_phone, is_used=False).order_by("-sent_at").first()
        if last:
            seconds_since = int((now - last.sent_at).total_seconds())
            if seconds_since < RESEND_COOLDOWN_SECONDS:
                wait_seconds = RESEND_COOLDOWN_SECONDS - seconds_since
                raise serializers.ValidationError({"new_phone": f"Please wait {wait_seconds}s before requesting OTP again."})

        code = f"{random.randint(100000, 999999)}"
        PhoneChangeOTP.objects.create(
            user=user,
            new_phone=new_phone,
            otp_hash=make_password(code),
            expires_at=now + timezone.timedelta(seconds=OTP_TTL_SECONDS),
            sent_at=now,
        )

        # TODO: send SMS to new_phone using provider
        print("DEV Phone Change OTP:", new_phone, code)

        return {"new_phone": new_phone, "expires_in": OTP_TTL_SECONDS, "resend_after": RESEND_COOLDOWN_SECONDS}


class ConfirmPhoneChangeSerializer(serializers.Serializer):
    new_phone = serializers.CharField()
    code = serializers.CharField(min_length=4, max_length=8)

    def validate_new_phone(self, value):
        value = normalize_e164(value)
        user = self.context["request"].user

        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("This phone number is already used by another account.")

        return value

    def create(self, validated_data):
        user = self.context["request"].user
        new_phone = validated_data["new_phone"]
        code = validated_data["code"].strip()
        now = timezone.now()

        otp = PhoneChangeOTP.objects.filter(
            user=user,
            new_phone=new_phone,
            is_used=False
        ).order_by("-created_at").first()

        if not otp:
            raise serializers.ValidationError({"code": "OTP not found. Please request again."})

        if now >= otp.expires_at:
            raise serializers.ValidationError({"code": "OTP expired. Please request again."})

        if otp.attempts >= 5:
            raise serializers.ValidationError({"code": "Too many attempts. Please request OTP again."})

        otp.attempts += 1
        otp.save(update_fields=["attempts"])

        if not check_password(code, otp.otp_hash):
            raise serializers.ValidationError({"code": "Invalid OTP."})

        otp.is_used = True
        otp.save(update_fields=["is_used"])

        # Update phone
        user.phone = new_phone
        user.save(update_fields=["phone"])

        return user