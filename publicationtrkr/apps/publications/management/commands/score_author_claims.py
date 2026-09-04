"""
Management command: score_author_claims

Recompute AuthorClaim suggestions for authors nobody has claimed yet (issue #32).

Phase 1 is entirely local -- project co-membership and name compatibility -- so a run
makes no network calls and costs one pass over unclaimed authors. See
publications/utils/claim_scoring.py for the signals and why they are weighted as they are.

What it will not do
-------------------
* **It never writes `Author.fabric_uuid`.** Scoring produces suggestions; only an admin
  approval or a self-claim writes attribution. That is the entire point of the issue.
* **It never touches a decided row.** `approved`, `rejected` and `self_asserted` claims
  are left exactly as they are -- a rejection that got re-suggested every night would
  make the queue useless, and rejections are also the only record of what an admin has
  already ruled out.
* **It never withdraws a decided row**, even when the pair no longer scores. A stale
  suggestion is withdrawn; a decision is history.

Usage:
    python manage.py score_author_claims              # unclaimed authors
    python manage.py score_author_claims --dry-run    # report only, no writes
    python manage.py score_author_claims --full       # rescore every author, claimed too
    python manage.py score_author_claims --if-due     # no-op unless the cadence elapsed
"""

import os
from datetime import datetime, timezone
from uuid import uuid4

from django.core.management.base import BaseCommand
from django.db import transaction

from publicationtrkr.apps.apiuser.models import ApiUser, TaskTimeoutTracker
from publicationtrkr.apps.apiuser.utils.locks import (
    CLAIM_SCORING_ADVISORY_LOCK_KEY,
    advisory_lock,
)
from publicationtrkr.apps.publications.models import Author, AuthorClaim, Publication
from publicationtrkr.apps.publications.utils.claim_scoring import candidates_for_author

# How many suggestions to keep per author. The queue is read by a human: the tenth-best
# candidate for one author has never been the right answer, and storing it only costs an
# admin the scroll.
MAX_SUGGESTIONS_PER_AUTHOR = 5


class Command(BaseCommand):
    help = 'Score candidate FABRIC users for unclaimed publication authors (#32).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report what would change without writing anything.',
        )
        parser.add_argument(
            '--full',
            action='store_true',
            help='Rescore every author, including ones that already have a fabric_uuid. '
                 'Normally only unclaimed authors are considered.',
        )
        parser.add_argument(
            '--if-due',
            action='store_true',
            help='Exit without scoring unless CLM_TIMEOUT_IN_SECONDS has elapsed since '
                 'the last run. Lets the sidecar fire more often than the cadence.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        full = options['full']
        if_due = options['if_due']

        tracker_name = os.getenv('CLM_NAME') or 'claim_scoring_check'
        tracker = TaskTimeoutTracker.objects.filter(name=tracker_name).first()
        if tracker is None:
            self.stdout.write(self.style.WARNING(
                'No "{0}" TaskTimeoutTracker row -- run init_task_timeout_tracker. '
                'Proceeding without a cadence.'.format(tracker_name)
            ))

        if if_due and tracker is not None and not tracker.timed_out():
            self.stdout.write(
                'Not due: last run {0}, cadence {1}s. Nothing to do.'.format(
                    tracker.last_updated, tracker.timeout_in_seconds
                )
            )
            return

        now = datetime.now(timezone.utc)
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN - no changes will be written.\n'))

        with advisory_lock(CLAIM_SCORING_ADVISORY_LOCK_KEY) as acquired:
            if not acquired:
                self.stdout.write(self.style.WARNING(
                    'Another score_author_claims run holds the lock. Exiting without '
                    'scoring.'
                ))
                return
            self._score(full=full, dry_run=dry_run, tracker=tracker, now=now)

    def _score(self, full, dry_run, tracker, now):
        authors = Author.objects.all() if full \
            else Author.objects.filter(fabric_uuid__isnull=True)
        authors = list(authors.order_by('id'))

        # Only active people are worth suggesting. sync_fabric_users marks departed people
        # inactive rather than deleting them, so without this the queue would offer people
        # who have left FABRIC. A nameless row cannot be name-matched at all.
        api_users = list(
            ApiUser.objects.filter(active=True).exclude(name='').order_by('id')
        )

        # One query for the publications these authors belong to rather than one per
        # author: Author carries publication_uuid as a plain string, so there is no
        # select_related to lean on.
        pub_uuids = {a.publication_uuid for a in authors}
        publications = {
            p.uuid: p for p in Publication.objects.filter(uuid__in=pub_uuids)
        }

        # Pairs an admin has already ruled on, so scoring can leave them alone.
        decided = {
            (c.author_id, c.api_user_id)
            for c in AuthorClaim.objects.filter(status__in=AuthorClaim.DECIDED_STATUSES)
        }
        existing = {
            (c.author_id, c.api_user_id): c
            for c in AuthorClaim.objects.filter(status=AuthorClaim.SUGGESTED)
        }

        self.stdout.write(
            'Authors        : {0} ({1})\n'
            'Candidates     : {2} active FABRIC users\n'
            'Already decided: {3} pair(s), left untouched\n'.format(
                len(authors), 'all' if full else 'unclaimed only',
                len(api_users), len(decided),
            )
        )

        created = updated = unchanged = skipped_decided = 0
        authors_with_suggestions = 0
        seen_pairs = set()

        for author in authors:
            publication = publications.get(author.publication_uuid)
            ranked = candidates_for_author(author, api_users, publication)
            ranked = ranked[:MAX_SUGGESTIONS_PER_AUTHOR]
            if ranked:
                authors_with_suggestions += 1

            for score, api_user, signals in ranked:
                key = (author.id, api_user.id)
                if key in decided:
                    skipped_decided += 1
                    continue
                seen_pairs.add(key)
                current = existing.get(key)
                if current is None:
                    created += 1
                    if not dry_run:
                        AuthorClaim.objects.create(
                            author=author, api_user=api_user, score=score,
                            signals=signals, source=AuthorClaim.MACHINE,
                            status=AuthorClaim.SUGGESTED, uuid=str(uuid4()),
                        )
                elif current.score != score or current.signals != signals:
                    updated += 1
                    if not dry_run:
                        current.score = score
                        current.signals = signals
                        current.save(update_fields=['score', 'signals', 'modified'])
                else:
                    unchanged += 1

        # A standing suggestion that no longer scores is withdrawn -- an author renamed
        # through the API should not keep the suggestions computed from the old spelling.
        # Only suggestions: a decision is never withdrawn by a machine.
        stale = [c for key, c in existing.items() if key not in seen_pairs]
        if stale and not dry_run:
            with transaction.atomic():
                AuthorClaim.objects.filter(id__in=[c.id for c in stale]).delete()

        self.stdout.write(
            '\n--- Summary ---\n'
            'Authors scored     : {0}\n'
            'With suggestions   : {1}\n'
            'Suggestions created: {2}\n'
            'Suggestions updated: {3}\n'
            'Unchanged          : {4}\n'
            'Withdrawn (stale)  : {5}\n'
            'Left decided       : {6}\n'.format(
                len(authors), authors_with_suggestions, created, updated,
                unchanged, len(stale), skipped_decided,
            )
        )

        if dry_run:
            self.stdout.write('Cadence      : not advanced (dry run)')
            return
        if tracker is not None:
            tracker.last_updated = now
            tracker.value = now.isoformat()
            tracker.save(update_fields=['last_updated', 'value'])
            self.stdout.write('Cadence      : advanced to {0}'.format(tracker.value))
