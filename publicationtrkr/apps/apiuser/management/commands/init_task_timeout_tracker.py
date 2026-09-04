import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from publicationtrkr.apps.apiuser.models import TaskTimeoutTracker

# (name, description, timeout) environment keys for each tracked task, each with the
# default to use when the variable is absent.
#
# The defaults are not decoration. Until 1.12.0 these were read with a bare
# `int(os.getenv(...))`, so a deployment that had not yet added the USR_* triple to its
# .env crashed this function on every boot -- the 1.12.0 deploy hazard. A new tracker
# must not be able to repeat that, so CLM_* ships with working defaults and the .env
# addition is optional. Existing keys keep their current values as defaults, which is
# a no-op where they are set and a repair where they are not.
TRACKERS = (
    ('PSK_NAME', 'public_signing_key',
     'PSK_DESCRIPTION', 'Public Signing Key',
     'PSK_TIMEOUT_IN_SECONDS', 86400),
    ('TRL_NAME', 'token_revocation_list',
     'TRL_DESCRIPTION', 'Token Revocation List',
     'TRL_TIMEOUT_IN_SECONDS', 300),
    ('USR_NAME', 'user_sync_check',
     'USR_DESCRIPTION', 'User Sync Check',
     'USR_TIMEOUT_IN_SECONDS', 86400),
    # Author-claim scoring (#32). 24h, matching the user sync it runs after.
    ('CLM_NAME', 'claim_scoring_check',
     'CLM_DESCRIPTION', 'Author Claim Scoring Check',
     'CLM_TIMEOUT_IN_SECONDS', 86400),
)


def init_task_timeout_tracker():
    """
    Initialize/Update the TaskTimeoutTracker table
    - public_signing_key
    - token_revocation_list
    - user_sync_check
    - claim_scoring_check

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
        for (name_key, name_default,
             description_key, description_default,
             timeout_key, timeout_default) in TRACKERS:
            timeout_in_seconds = int(os.getenv(timeout_key) or timeout_default)
            shared = {
                'description': os.getenv(description_key) or description_default,
                'timeout_in_seconds': timeout_in_seconds,
            }
            TaskTimeoutTracker.objects.update_or_create(
                name=os.getenv(name_key) or name_default,
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
