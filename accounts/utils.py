from django.core import signing

SIGNUP_SALT = "chatapp.signup"
SIGNUP_MAX_AGE_SECONDS = 10 * 60  # 10 minutes

def make_signup_token(phone: str) -> str:
    return signing.dumps({"phone": phone}, salt=SIGNUP_SALT)

def read_signup_token(token: str) -> str:
    data = signing.loads(token, salt=SIGNUP_SALT, max_age=SIGNUP_MAX_AGE_SECONDS)
    return data["phone"]
