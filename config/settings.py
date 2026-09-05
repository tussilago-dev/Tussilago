from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from config.settings_helpers import get_data_dir

logger: logging.Logger = logging.getLogger("tussilago.settings")

load_dotenv(verbose=True)


BASE_DIR: Path = Path(__file__).resolve().parent.parent
DATA_DIR: Path = get_data_dir()

SECRET_KEY: str = os.environ.get(key="DJANGO_SECRET_KEY", default="django-insecure-1234567890")
DEBUG: bool = os.environ.get(key="DJANGO_DEBUG", default="True").lower() == "true"
ALLOWED_HOSTS: list[str] = []
ROOT_URLCONF: str = "config.urls"
WSGI_APPLICATION: str = "config.wsgi.application"
LANGUAGE_CODE: str = "en-us"
TIME_ZONE: str = "UTC"
USE_I18N: bool = True
USE_TZ: bool = True

STATIC_ROOT: Path = DATA_DIR / "staticfiles"
STATIC_ROOT.mkdir(parents=True, exist_ok=True)
STATIC_URL = "/static/"
STATICFILES_DIRS: list[Path] = [BASE_DIR / "static"]

ADMINS: list[tuple[str, str]] = [("Joakim Hellsén", "tlovinator@gmail.com")]


INSTALLED_APPS: list[str] = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE: list[str] = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


TEMPLATES: list[dict[str, Any]] = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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
EMAIL_SUBJECT_PREFIX = "[Tussilago] "
EMAIL_USE_LOCALTIME = True
SERVER_EMAIL: str | None = os.getenv(key="EMAIL_HOST_USER", default=None)

MAILERS: dict[str, dict[str, Any]] = {
    "default": {
        "BACKEND": "django.core.mail.backends.console.EmailBackend",
        "OPTIONS": {
            "host": str(os.getenv(key="EMAIL_HOST", default="smtp.gmail.com")),
            "use_tls": bool(os.getenv(key="EMAIL_USE_TLS", default="True").lower() == "true"),
            "username": str(os.getenv(key="EMAIL_HOST_USER", default=None)),
            "password": str(os.getenv(key="EMAIL_HOST_PASSWORD", default=None)),
            "timeout": int(os.getenv(key="EMAIL_TIMEOUT", default="5")),
        },
    },
}
