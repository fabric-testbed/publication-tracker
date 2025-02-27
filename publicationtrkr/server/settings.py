"""
Django settings for publicationtrkr project.

Generated by 'django-admin startproject' using Django 5.1.1.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/5.1/ref/settings/
"""

import os
from corsheaders.defaults import default_headers
from pathlib import Path

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
if os.getenv('DJANGO_SECRET_KEY'):
    SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
else:
    SECRET_KEY = 'django-insecure-58k5p&d&6ztt_i=l8$(94i&%s%h-kpznyu8^7m1pd=*wh@x^kr'

# SECURITY WARNING: don't run with debug turned on in production!
if os.getenv('DJANGO_DEBUG').casefold() == 'true':
    DEBUG = True
else:
    DEBUG = False

# SECURITY WARNING: don't run with API debug turned on in production!
if os.getenv('API_DEBUG').casefold() == 'true':
    API_DEBUG = True
else:
    API_DEBUG = False

# Update hosts for production use
ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
]

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'corsheaders',  # django cors headers
    'drf_spectacular',
    'django_bootstrap5',  # django bootstrap
    'fontawesomefree',  # fontawesome free version
    'publicationtrkr.apps.apiuser',  # app: apiuser
    'publicationtrkr.apps.pubsimple',  # app: pubsimple
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'corsheaders.middleware.CorsMiddleware',
]

CORS_ALLOWED_ORIGINS = [
    'https://127.0.0.1',
    'https://localhost',
]

CORS_ALLOW_METHODS = (
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
)

CORS_ALLOW_HEADERS = (
    "accept",
    "authorization",
    "content-type",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    "DNT",
    "User-Agent",
    "X-Requested-With",
    "If-Modified-Since",
    "Cache-Control",
    "Range",
)

CORS_AlLOW_CREDENTIALS = True

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': int(os.getenv('REST_FRAMEWORK_PAGE_SIZE')),
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
}

SPECTACULAR_SETTINGS = {
    # 'PREPROCESSING_HOOKS': ['artifactmgr.server.api_filters.preprocessing_filter_spec'],
    'TITLE': 'FABRIC Publication Tracker',
    'DESCRIPTION': 'A platform for sharing FABRIC related publications.',
    'VERSION': '0.0.1',
    'SERVE_INCLUDE_SCHEMA': False,
    # OTHER SETTINGS
    'COMPONENT_SPLIT_REQUEST': True,
}

ROOT_URLCONF = 'publicationtrkr.server.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            os.path.join(BASE_DIR, 'publicationtrkr/templates'),
            os.path.join(BASE_DIR, 'publicationtrkr/templates/publicationtrkr'),
            os.path.join(BASE_DIR, 'publicationtrkr/templates/pubsimple'),
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'publicationtrkr.server.wsgi.application'

# Database
# https://docs.djangoproject.com/en/5.1/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.getenv('POSTGRES_DB'),
        'USER': os.getenv('POSTGRES_USER'),
        'PASSWORD': os.getenv('POSTGRES_PASSWORD'),
        'HOST': os.getenv('POSTGRES_HOST'),
        'PORT': os.getenv('POSTGRES_PORT')
    }
}

# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True

# Login / Logout settings
LOGIN_URL = '/login'
LOGIN_REDIRECT_URL = '/'

LOGOUT_URL = '/logout'
LOGOUT_REDIRECT_URL = '/'

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/

STATIC_URL = '/static/'
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'publicationtrkr/server/static'),
]
STATIC_ROOT = os.path.join(BASE_DIR, 'static')

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# Default primary key field type
# https://docs.djangoproject.com/en/5.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
