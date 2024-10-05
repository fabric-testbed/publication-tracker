import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from django.core.management.base import BaseCommand, CommandError

from publicationtrkr.apps.apiuser.models import TaskTimeoutTracker


def init_task_timeout_tracker():
    """
    Initialize/Update the TaskTimeoutTracker table
    - author_refresh_check
    - public_signing_key
    - token_revocation_list
    """
    try:
        now = datetime.now(timezone.utc)
        # author_refresh_check
        arc = TaskTimeoutTracker.objects.filter(name=os.getenv('ARC_NAME')).first()
        if not arc:
            arc = TaskTimeoutTracker(
                description=os.getenv('ARC_DESCRIPTION'),
                last_updated=(now - timedelta(seconds=(int(os.getenv('ARC_TIMEOUT_IN_SECONDS')) + 1))),
                name=os.getenv('ARC_NAME'),
                timeout_in_seconds=int(os.getenv('ARC_TIMEOUT_IN_SECONDS')),
                uuid=str(uuid4()),
                value=None
            )
        else:
            arc.description = os.getenv('ARC_DESCRIPTION')
            arc.name = os.getenv('ARC_NAME')
            arc.timeout_in_seconds = os.getenv('ARC_TIMEOUT_IN_SECONDS')
        arc.save()
        # public_signing_key
        psk = TaskTimeoutTracker.objects.filter(name=os.getenv('PSK_NAME')).first()
        if not psk:
            psk = TaskTimeoutTracker(
                description=os.getenv('PSK_DESCRIPTION'),
                last_updated=(now - timedelta(seconds=(int(os.getenv('PSK_TIMEOUT_IN_SECONDS')) + 1))),
                name=os.getenv('PSK_NAME'),
                timeout_in_seconds=int(os.getenv('PSK_TIMEOUT_IN_SECONDS')),
                uuid=str(uuid4()),
                value=None
            )
        else:
            psk.description = os.getenv('PSK_DESCRIPTION')
            psk.name = os.getenv('PSK_NAME')
            psk.timeout_in_seconds = os.getenv('PSK_TIMEOUT_IN_SECONDS')
        psk.save()
        # token_revocation_list
        trl = TaskTimeoutTracker.objects.filter(name=os.getenv('TRL_NAME')).first()
        if not trl:
            trl = TaskTimeoutTracker(
                description=os.getenv('TRL_DESCRIPTION'),
                last_updated=(now - timedelta(seconds=(int(os.getenv('TRL_TIMEOUT_IN_SECONDS')) + 1))),
                name=os.getenv('TRL_NAME'),
                timeout_in_seconds=int(os.getenv('TRL_TIMEOUT_IN_SECONDS')),
                uuid=str(uuid4()),
                value=None
            )
        else:
            trl.description = os.getenv('TRL_DESCRIPTION')
            trl.name = os.getenv('TRL_NAME')
            trl.timeout_in_seconds = os.getenv('TRL_TIMEOUT_IN_SECONDS')
        trl.save()
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
