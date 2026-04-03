from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import permissions
from rest_framework_simplejwt.tokens import RefreshToken

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
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        auth = request.headers.get("Authorization", "")
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
        return Response({
            "id": str(u.id),
            "phone": u.phone,
            "full_name": u.full_name,
            "bio": u.bio,
        })
