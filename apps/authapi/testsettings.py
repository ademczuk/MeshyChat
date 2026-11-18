"""Minimal Django settings for exercising the auth routes.

Deliberately NOT `meshychat.settings`: that module is another agent's file and points
DATABASES at sqlite, which this surface never uses — every query here runs through
psycopg against the real Postgres (apps/authapi/db.py). Django is used for URL routing,
request parsing and response encoding only, so the settings it needs are the ones that
make `django.test.Client` work and nothing else.
"""
from __future__ import annotations

SECRET_KEY = "authapi-tests-not-a-production-key"
DEBUG = False
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "apps.authapi.urls"
INSTALLED_APPS: list[str] = []
MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
DATABASES: dict = {}
USE_TZ = True
TIME_ZONE = "UTC"
# APPEND_SLASH would 301 a POST to /v1/auth/sign-in if a route ever went missing,
# turning a routing bug into a redirect that looks like a client error.
APPEND_SLASH = False
DEFAULT_CHARSET = "utf-8"
