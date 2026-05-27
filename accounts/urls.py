from django.urls import path
from .views import SendOTPView, VerifyOTPView, CompleteSignupView, MeView,EditProfileView

urlpatterns = [
    path("otp/send/", SendOTPView.as_view()),
    path("otp/verify/", VerifyOTPView.as_view()),
    path("signup/complete/", CompleteSignupView.as_view()),
    path("me/", MeView.as_view()),
    path("profile/edit/", EditProfileView.as_view()),

]
