from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework import status
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from .serializers import SendOTPSerializer, VerifyOTPSerializer, CompleteSignupSerializer
from .utils import make_signup_token,read_signup_token
def jwt_tokens(user):
    refresh = RefreshToken.for_user(user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
    }

class SendOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        s = SendOTPSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = s.save()
        return Response({"success": True, "data": data})


class VerifyOTPView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        s = VerifyOTPSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        result = s.save()

        user = result["user"]
        needs_profile = result["needs_profile"]

        # ✅ EXISTING USER => full login tokens
        if not needs_profile:
            return Response({
                "success": True,
                "type": "login",
                "tokens": jwt_tokens(user),
                "user": {
                    "id": str(user.id),
                    "phone": user.phone,
                    "full_name": user.full_name,
                    "bio": user.bio,
                }
            })

        # ✅ NEW USER => signup_token (short-lived, only for /signup/complete)
        return Response({
            "success": True,
            "type": "signup",
            "needs_profile": True,
            "signup_token": make_signup_token(user.phone),
        })

class CompleteSignupView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        print(request)
        auth = request.headers.get("Authorization", "")
        print(auth)
        print(auth)
        if not auth.startswith("Bearer "):
            return Response({"detail": "Missing signup token"}, status=401)

        signup_token = auth.split(" ", 1)[1].strip()

        try:
            phone = read_signup_token(signup_token)
        except Exception:
            return Response({"detail": "Invalid or expired signup token"}, status=401)

        s = CompleteSignupSerializer(data=request.data, context={"phone": phone})
        s.is_valid(raise_exception=True)
        user = s.save()

        # ✅ after profile complete => full JWT login tokens
        return Response({
            "success": True,
            "type": "signup_completed",
            "tokens": jwt_tokens(user),
            "user": {
                "id": str(user.id),
                "phone": user.phone,
                "full_name": user.full_name,
                "bio": user.bio,
            }
        })

class MeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        u = request.user
        image = getattr(u, "profile_picture", None)
        image_url = ""
        if image:
            image_url = request.build_absolute_uri(image.url)
             
        
        return Response({
            "id": str(u.id),
            "phone": u.phone,
            "full_name": u.full_name,
            "profile_picture":image_url,
            "bio": u.bio,
        })
class EditProfileView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request):
        user = request.user

        user.full_name = request.data.get(
            "full_name",
            user.full_name,
        )

        user.bio = request.data.get(
            "bio",
            user.bio,
        )

        if "profile_picture" in request.FILES:
            user.profile_picture = request.FILES["profile_picture"]

        user.save()

        profile_picture_url = ""

        if user.profile_picture:
            try:
                profile_picture_url = request.build_absolute_uri(
                    user.profile_picture.url
                )
            except Exception:
                profile_picture_url = user.profile_picture.url

        return Response({
            "success": True,
            "user": {
                "id": str(user.id),
                "phone": user.phone,
                "full_name": user.full_name,
                "bio": user.bio,
                "profile_picture": profile_picture_url,
            }
        })


