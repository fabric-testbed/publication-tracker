"""Isolated PostgreSQL test configuration; never loads deployment .env files.

Only TEST_POSTGRES_* variables select the disposable database. Application
credentials and remote URLs are synthetic, even if deployment variables exist.
"""

import os

os.environ.update({
    'DJANGO_SECRET_KEY': 'publication-tracker-tests-only-not-a-deployment-secret',
    'DJANGO_DEBUG': 'false',
    'API_DEBUG': 'false',
    'REST_FRAMEWORK_PAGE_SIZE': '20',
    'PUBLICATION_TRACKER_ADMINS_ROLE': 'publication-tracker-admins',
    'CAN_CREATE_PUBLICATION_ROLE': 'Jupyterhub',
    'API_USER_REFRESH_CHECK_MINUTES': '5',
    'API_USER_ANON_UUID': '00000000-0000-0000-0000-000000000000',
    'API_USER_ANON_NAME': 'Anonymous API User',
    'FABRIC_CORE_API': 'https://core-api.invalid',
    'FABRIC_CREDENTIAL_MANAGER': 'https://credential-manager.invalid',
    'FABRIC_PORTAL': 'https://portal.invalid',
    'FABRIC_CORE_API_TOKEN': 'test-readonly-token',
    'FABRIC_CORE_API_SERVICES_TOKEN': 'test-services-token',
    'FABRIC_TOKEN_ISSUER': '',
    'FABRIC_TOKEN_AUDIENCE': '',
    'VOUCH_COOKIE_NAME': 'test-vouch-cookie',
    'VOUCH_COOKIE_DOMAIN': '',
    'VOUCH_JWT_SECRET': 'test-vouch-secret-that-is-not-valid-in-production',
    'VOUCH_JWT_ISSUER': 'Vouch',
    'VOUCH_JWT_AUDIENCE': '',
    'PSK_NAME': 'public_signing_key',
    'PSK_DESCRIPTION': 'Public Signing Key',
    'PSK_TIMEOUT_IN_SECONDS': '86400',
    'TRL_NAME': 'token_revocation_list',
    'TRL_DESCRIPTION': 'Token Revocation List',
    'TRL_TIMEOUT_IN_SECONDS': '300',
    'USR_NAME': 'user_sync_check',
    'USR_DESCRIPTION': 'User Sync Check',
    'USR_TIMEOUT_IN_SECONDS': '86400',
})

from publicationtrkr.server.settings import *  # noqa: E402,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('TEST_POSTGRES_DB', 'publication_tracker_test'),
        'USER': os.getenv('TEST_POSTGRES_USER', 'publication_tracker_test'),
        'PASSWORD': os.getenv('TEST_POSTGRES_PASSWORD', 'test-only-password'),
        'HOST': os.getenv('TEST_POSTGRES_HOST', '127.0.0.1'),
        'PORT': os.getenv('TEST_POSTGRES_PORT', '55439'),
    },
}
ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
# Do not accumulate rate limits across unrelated API cases that all use the
# test client's loopback address. Throttle tests must opt in explicitly.
REST_FRAMEWORK = {**REST_FRAMEWORK, 'DEFAULT_THROTTLE_CLASSES': []}  # noqa: F405
