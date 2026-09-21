#!/usr/bin/env bash
# Run against a disposable PostgreSQL database using synthetic app credentials.
# Configure TEST_POSTGRES_* only; deployment .env files are never sourced.
set -euo pipefail
cd "$(dirname "$0")/.."
TEST_PYTHON="${TEST_PYTHON:-.venv/bin/python}"
export DJANGO_SETTINGS_MODULE=publicationtrkr.server.test_settings
"$TEST_PYTHON" manage.py check
"$TEST_PYTHON" manage.py makemigrations --check --dry-run
"$TEST_PYTHON" manage.py test --noinput "$@"
