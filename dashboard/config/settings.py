from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from django.contrib import messages
from dotenv import load_dotenv

from config.settings_helpers import get_data_dir

logger: logging.Logger = logging.getLogger("tussilago.settings")

load_dotenv(verbose=True)


BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = get_data_dir()

SECRET_KEY: str = os.environ.get(key="DJANGO_SECRET_KEY", default="django-insecure-1234567890")
DEBUG: bool = os.environ.get(key="DJANGO_DEBUG", default="True").lower() == "true"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
ROOT_URLCONF: str = "config.urls"
WSGI_APPLICATION: str = "config.wsgi.application"
LANGUAGE_CODE: str = "en-us"
TIME_ZONE: str = "UTC"
USE_I18N: bool = True
USE_TZ: bool = True
SITE_ID = 1

if DEBUG:
    ALLOWED_HOSTS: list[str] = ["127.0.0.1", "localhost"]
else:
    ALLOWED_HOSTS: list[str] = ["tussilago.dev"]

CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

STATIC_ROOT: Path = DATA_DIR / "staticfiles"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)
STATIC_URL: str = "/static/"
STATICFILES_DIRS: list[Path] = [BASE_DIR / "static"]

ADMINS: list[tuple[str, str]] = [("Joakim Hellsén", "tlovinator@gmail.com")]
MANAGERS: list[tuple[str, str]] = ADMINS

ACCOUNT_LOGIN_BY_CODE_ENABLED = True
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED = True
ACCOUNT_LOGIN_METHODS: set[str] = {"email", "username"}
ACCOUNT_PASSWORD_RESET_BY_CODE_ENABLED = True
ACCOUNT_SIGNUP_FIELDS: list[str] = ["username*", "email*", "password1*", "password2*"]
LOGIN_REDIRECT_URL: str = "profile"
ACCOUNT_SIGNUP_FORM_CLASS = "tussilago.forms.SignUpForm"
SOCIALACCOUNT_QUERY_EMAIL = True
USERSESSIONS_TRACK_ACTIVITY: bool = True

MFA_SUPPORTED_TYPES: list[str] = [
    "webauthn",
    "totp",
    "recovery_codes",
]
MFA_PASSKEY_LOGIN_ENABLED: bool = True
MFA_PASSKEY_SIGNUP_ENABLED: bool = True

STATICFILES_FINDERS = (
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
)

AUTHENTICATION_BACKENDS: list[str] = [
    "allauth.account.auth_backends.AuthenticationBackend",
]
INSTALLED_APPS: list[str] = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.windowslive",
    "allauth.socialaccount.providers.google",
    "allauth.usersessions",
    "allauth.idp.oidc",
    "allauth.mfa",
    "crispy_forms",
    "crispy_bootstrap5",
]

MIDDLEWARE: list[str] = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "allauth.usersessions.middleware.UserSessionsMiddleware",
]


TEMPLATES: list[dict[str, Any]] = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [
            BASE_DIR / "templates",
        ],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


DATABASES: dict[str, Any] = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": str(os.getenv(key="TUSSILAGO_POSTGRES_DB", default="tussilago")),
        "USER": str(os.getenv(key="TUSSILAGO_POSTGRES_USER", default="tussilago")),
        "PASSWORD": str(os.getenv(key="TUSSILAGO_POSTGRES_PASSWORD", default="")),
        "HOST": str(os.getenv(key="TUSSILAGO_POSTGRES_HOST", default="localhost")),
        "PORT": str(os.getenv(key="TUSSILAGO_POSTGRES_PORT", default="5432")),
        "OPTIONS": {
            "pool": True,
        },
    },
}

AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


DEFAULT_FROM_EMAIL: str | None = os.getenv(key="EMAIL_HOST_USER", default=None)
EMAIL_SUBJECT_PREFIX: str = "[Tussilago] "
EMAIL_USE_LOCALTIME: bool = True
SERVER_EMAIL: str | None = os.getenv(key="EMAIL_HOST_USER", default=None)


# If DEBUG is True, use the console email backend instead of sending real emails
mail_backend = "django.core.mail.backends.smtp.EmailBackend"
if DEBUG:
    mail_backend = "django.core.mail.backends.console.EmailBackend"

MAILERS: dict[str, dict[str, Any]] = {
    "default": {
        "BACKEND": mail_backend,
    },
}

if not DEBUG:
    MAILERS["default"]["OPTIONS"] = {
        "host": os.getenv("EMAIL_HOST", "smtp.gmail.com"),
        "use_tls": os.getenv("EMAIL_USE_TLS", "True").lower() == "true",
        "username": os.getenv("EMAIL_HOST_USER"),
        "password": os.getenv("EMAIL_HOST_PASSWORD"),
        "timeout": int(os.getenv("EMAIL_TIMEOUT", "5")),
    }

MESSAGE_TAGS = {
    messages.DEBUG: "alert-info",
    messages.INFO: "alert-info",
    messages.SUCCESS: "alert-success",
    messages.WARNING: "alert-warning",
    messages.ERROR: "alert-danger",
}

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "SCOPE": [
            "profile",
            "email",
        ],
        "AUTH_PARAMS": {
            "access_type": "online",
        },
    },
    "github": {
        "SCOPE": [
            "user",
            "emails",
            "read:org",
        ],
    },
}

# Read file from the path specified in the environment variable IDP_OIDC_PRIVATE_KEY_PATH
IDP_OIDC_PRIVATE_KEY_PATH: str | None = os.getenv(key="IDP_OIDC_PRIVATE_KEY_PATH", default=None)
IDP_OIDC_PRIVATE_KEY: str | None = None
if IDP_OIDC_PRIVATE_KEY_PATH:
    IDP_OIDC_PRIVATE_KEY = Path(IDP_OIDC_PRIVATE_KEY_PATH).read_text(encoding="utf-8")
