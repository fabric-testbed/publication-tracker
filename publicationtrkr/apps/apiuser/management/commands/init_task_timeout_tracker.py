import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from publicationtrkr.apps.apiuser.models import TaskTimeoutTracker

# (name, description, timeout) environment keys for each tracked task
TRACKERS = (
    ('PSK_NAME', 'PSK_DESCRIPTION', 'PSK_TIMEOUT_IN_SECONDS'),
    ('TRL_NAME', 'TRL_DESCRIPTION', 'TRL_TIMEOUT_IN_SECONDS'),
    ('USR_NAME', 'USR_DESCRIPTION', 'USR_TIMEOUT_IN_SECONDS'),
)


def init_task_timeout_tracker():
    """
    Initialize/Update the TaskTimeoutTracker table
    - public_signing_key
    - token_revocation_list
    - user_sync_check

    ARC / author_refresh_check is deliberately gone: it was scaffolding for an
    abandoned earlier attempt at user sync, initialized on every boot and read by
    nothing. Migration 0003 removes the row it left on deployed databases.

    Keyed on name via update_or_create so re-running against a database that
    already holds these rows updates them in place. The previous
    .filter(name=...).first() pattern inserted a second row whenever the lookup
    missed, and the duplicates it left behind broke the objects.get(name=...)
    calls in utils/fabric_auth.py.
    """
    try:
        now = datetime.now(timezone.utc)
        for name_key, description_key, timeout_key in TRACKERS:
            timeout_in_seconds = int(os.getenv(timeout_key))
            shared = {
                'description': os.getenv(description_key),
                'timeout_in_seconds': timeout_in_seconds,
            }
            TaskTimeoutTracker.objects.update_or_create(
                name=os.getenv(name_key),
                defaults=shared,
                create_defaults={
                    **shared,
                    # start out timed out so the first caller populates the value
                    'last_updated': now - timedelta(seconds=timeout_in_seconds + 1),
                    'uuid': str(uuid4()),
                    'value': None,
                },
            )
    except Exception as exc:
        print(exc)


class Command(BaseCommand):
    help = 'Initialize/Update the TaskTimeoutTracker table'

    def handle(self, *args, **kwargs):
        try:
            init_task_timeout_tracker()

        except Exception as e:
            print(e)
            raise CommandError('Initialization failed.')
