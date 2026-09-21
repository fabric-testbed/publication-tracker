"""Preview or apply whitespace-only directory name cleanup; never reconcile accounts."""

from contextlib import nullcontext

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import Author
from publicationtrkr.utils.names import normalize_person_name


class Command(BaseCommand):
    help = 'Preview directory name whitespace cleanup; use --apply to save it. UUIDs and attributions stay unchanged.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Save whitespace-normalized names atomically.')

    def handle(self, *args, **options):
        apply = options['apply']
        changes = []
        with transaction.atomic() if apply else nullcontext():
            users = ApiUser.objects.order_by('pk')
            if apply:
                # Read current names after taking row locks, so a concurrent login
                # refresh or directory sync cannot be overwritten with stale data.
                users = users.select_for_update()
            users = list(users)
            references = dict(
                Author.objects.exclude(fabric_uuid__isnull=True)
                .order_by().values('fabric_uuid').annotate(total=Count('pk'))
                .values_list('fabric_uuid', 'total')
            )
            for user in users:
                normalized = normalize_person_name(user.name)
                if normalized == user.name:
                    continue
                changes.append((user.uuid, user.name, normalized, references.get(user.uuid, 0)))
                if apply:
                    user.name = normalized
                    user.save(update_fields=['name'])

        self.stdout.write('APPLY' if apply else 'DRY RUN — use --apply to save changes.')
        for uuid, before, after, reference_count in changes:
            self.stdout.write(
                '{0}: {1!r} -> {2!r}; Author.fabric_uuid references: {3}'
                .format(uuid, before, after, reference_count)
            )
        self.stdout.write(
            '{0} {1} name(s); {2} attribution reference(s) preserved.'
            .format('Normalized' if apply else 'Would normalize', len(changes),
                    sum(change[3] for change in changes))
        )
