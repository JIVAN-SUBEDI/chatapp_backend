from django.db import models
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin, BaseUserManager
from django.utils import timezone

class UserManager(BaseUserManager):
    def create_user(self, phone, full_name="", **extra_fields):
        if not phone:
            raise ValueError("Phone number is required")

        phone = phone.strip().replace(" ", "")
        user = self.model(phone=phone, full_name=full_name, **extra_fields)

        # No password system (OTP only)
        user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, phone, full_name="Admin", **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        return self.create_user(phone=phone, full_name=full_name, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    # Core identity
    full_name = models.CharField(max_length=150)
    phone = models.CharField(max_length=20, unique=True, db_index=True)

    # Profile
    bio = models.TextField(blank=True)
    profile_picture = models.ImageField(upload_to="profiles/", blank=True, null=True)

    # OTP (store hashed otp, never store plain otp)
    otp_hash = models.CharField(max_length=128, blank=True, null=True)
    otp_expires_at = models.DateTimeField(blank=True, null=True)
    otp_attempts = models.PositiveSmallIntegerField(default=0)

    # Status
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["full_name"]

    objects = UserManager()

    def __str__(self):
        return f"{self.phone} ({self.full_name})"

    def otp_is_valid_now(self) -> bool:
        return bool(self.otp_hash) and bool(self.otp_expires_at) and timezone.now() < self.otp_expires_at
    
class PhoneChangeOTP(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="phone_change_otps")
    new_phone = models.CharField(max_length=20, db_index=True)

    otp_hash = models.CharField(max_length=128)
    expires_at = models.DateTimeField()
    sent_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveSmallIntegerField(default=0)
    is_used = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "new_phone", "-created_at"]),
        ]

