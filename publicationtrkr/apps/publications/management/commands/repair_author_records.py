"""
Management command: repair_author_records

Apply the reviewed data repairs from issue #62 to the publications whose stored author
list disagrees with the published record.

Why this is a command and not a migration
-----------------------------------------
The repair has to go through `publication_builder.update_publication()`, which is the one
place that keeps `Publication.authors` and `Author.author_order` in step (#61). A data
migration cannot call it -- migrations see historical models, and update_publication
imports the real ones -- so it would have to reimplement the update path against a frozen
schema and drift from it. A command also gets two things a migration cannot: it can be
dry-run against production before it is trusted, and it can be run twice without harm.

What the plan file is
---------------------
Every edit is declared in `publications/data/issue_62_author_repairs.json`, reviewed as
part of the pull request rather than typed at a shell. Each publication entry carries

  * `expect_authors` -- the author names as they were surveyed. The entry is refused if
    the live row no longer matches, because #62's own instruction is to re-run the
    queries before acting: a list that has changed since the survey has not been
    reviewed.
  * `authors` -- the target list, in the order the paper prints it, each with the
    `fabric_uuid` that must end up attached to that slot.
  * `fields` -- optional publication fields to write (bibtex, link, title).

An entry whose target list is identical to what is stored is a *verification*: #62 asked
for 33 records to be checked by hand, and the ones that turned out to be right are worth
recording as checked rather than dropping off the list silently.

Four rules this command keeps
-----------------------------
**Nobody loses attribution.** Before writing anything it compares the set of fabric_uuids
currently on the publication with the set the plan asks for, and refuses the entry if the
plan would drop one. Attribution that has to change *slot* is moved, not cleared and
re-typed: `claim_ledger.relocate_claims()` carries the AuthorClaim row over with its
uuid, status and source intact, so a `self_asserted` claim is still self-asserted
afterwards.

**Nobody gains attribution either.** A fabric_uuid the publication does not already carry
is refused. Writing one would mean deciding a claim, and a decision belongs on the claim
queue or the author edit form, where a human makes it and the ledger records who did.

**No decided claim is deleted.** A row about to be dropped as surplus is checked for
decided claims first and the entry is refused if it has any. `suggested` rows are
withdrawn, which is what every other path in the app already does to a suggestion whose
author was renamed -- the next scoring run recomputes them.

**display_name follows author_name.** The web UI renders `display_name`, not
`author_name` (publication_detail.html, publication_list.html), and `_sync_authors` only
writes `author_name`. Repairing one without the other would fix the API and leave the
page still showing "Grigoryan, Garegin, Kevin Penkowski".

Usage:
    python manage.py repair_author_records                      # dry run, writes nothing
    python manage.py repair_author_records --check              # acceptance criteria only
    python manage.py repair_author_records --apply --as-user <fabric_uuid>
    python manage.py repair_author_records --only <publication_uuid> [--only ...]
"""

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import Author, AuthorClaim, Publication
from publicationtrkr.apps.publications.utils.claim_ledger import (
    relocate_claims,
    withdraw_suggestions,
)
from publicationtrkr.apps.publications.utils.publication_builder import update_publication
from publicationtrkr.apps.publications.utils.author_mutations import snapshot, record_correction

DEFAULT_PLAN = Path(__file__).resolve().parents[2] / 'data' / 'issue_62_author_repairs.json'

# The three shapes #62's first acceptance criterion rules out of an author_name.
FORBIDDEN_SUBSTRINGS = (';', ' and ')
FORBIDDEN_EXACT = ('others',)


class Command(BaseCommand):
    help = 'Apply the reviewed author-record repairs from issue #62.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--plan',
            default=str(DEFAULT_PLAN),
            help='Path to the repair plan JSON. Defaults to the reviewed plan in the app.',
        )
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Commit the repairs. Without this the command rolls back everything it '
                 'does, so a dry run exercises the real update path rather than a '
                 'simulation of it.',
        )
        parser.add_argument(
            '--as-user',
            dest='as_user',
            default=os.getenv('REPAIR_AS_FABRIC_UUID'),
            help='fabric_uuid of the publication-tracker admin the edits are recorded '
                 'against (Publication.modified_by). Required with --apply.',
        )
        parser.add_argument(
            '--only',
            action='append',
            default=[],
            dest='only',
            help='Repair just this publication uuid. Repeatable.',
        )
        parser.add_argument(
            '--check',
            action='store_true',
            help='Report the #62 acceptance criteria against the live data and exit.',
        )

    def handle(self, *args, **options):
        if options['check']:
            self._report_criteria()
            return

        plan = self._load_plan(options['plan'])
        apply_changes = options['apply']
        api_user = self._resolve_api_user(options['as_user'], apply_changes)

        only = set(options['only'])
        entries = [entry for entry in plan.get('publications', [])
                   if not only or entry['publication_uuid'] in only]
        orphans = [entry for entry in plan.get('orphan_authors', [])
                   if not only or entry['publication_uuid'] in only]

        header = 'APPLY' if apply_changes else 'DRY RUN (nothing will be committed)'
        self.stdout.write(self.style.MIGRATE_HEADING(
            'repair_author_records -- {0}'.format(header)))
        self.stdout.write('plan: {0}'.format(options['plan']))
        self.stdout.write('acting as: {0} ({1})'.format(api_user.uuid, api_user.name))
        self.stdout.write('')

        repaired = verified = 0
        with transaction.atomic():
            # Match the API's publication -> author lock order across the entire plan.
            publication_ids = {e['publication_uuid'] for e in entries + orphans}
            list(Publication.objects.select_for_update().filter(uuid__in=publication_ids).order_by('pk'))
            list(Author.objects.select_for_update().filter(publication_uuid__in=publication_ids).order_by('pk'))
            for entry in entries:
                changed = self._repair_publication(entry, api_user)
                repaired += 1 if changed else 0
                verified += 0 if changed else 1
            for entry in orphans:
                self._delete_orphan(entry, api_user)
            self._report_criteria()
            if not apply_changes:
                transaction.set_rollback(True)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            '{0} publication(s) repaired, {1} verified unchanged.'.format(
                repaired, verified)))
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'Dry run -- every change above was rolled back. Re-run with --apply '
                '--as-user <fabric_uuid> to commit.'))

    # -- plan and operator -------------------------------------------------------

    def _load_plan(self, path):
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise CommandError('No repair plan at {0}'.format(path))
        except json.JSONDecodeError as exc:
            raise CommandError('Repair plan is not valid JSON: {0}'.format(exc))

    def _resolve_api_user(self, fabric_uuid, apply_changes):
        """
        The admin the repair is recorded against. Required for --apply so that
        Publication.modified_by names a person rather than whatever account happened to be
        first in the table; a dry run falls back to any admin purely so the run can get
        far enough to be useful.
        """
        if fabric_uuid:
            api_user = ApiUser.objects.filter(uuid=fabric_uuid).first()
            if api_user is None:
                raise CommandError('No FABRIC user with uuid {0}'.format(fabric_uuid))
            if not api_user.is_publication_tracker_admin:
                raise CommandError(
                    '{0} is not a publication tracker admin; these repairs move '
                    'attribution and must be recorded against one.'.format(fabric_uuid))
            return api_user
        if apply_changes:
            raise CommandError('--apply requires --as-user <fabric_uuid> of an admin.')
        admin_role = os.getenv('PUBLICATION_TRACKER_ADMINS_ROLE') or ''
        api_user = ApiUser.objects.filter(fabric_roles__contains=[admin_role]).first()
        if api_user is None:
            raise CommandError(
                'No publication tracker admin found to dry-run as; pass --as-user.')
        return api_user

    # -- one publication ---------------------------------------------------------

    def _rows_in_order(self, publication):
        """
        The publication's Author rows in `Publication.authors` order, which is the
        authoritative order (#61) -- not Author.Meta.ordering, which is derived from it.
        """
        by_uuid = {a.uuid: a for a in Author.objects.filter(uuid__in=publication.authors)}
        return [by_uuid[u] for u in publication.authors if u in by_uuid]

    def _repair_publication(self, entry, api_user):
        pub_uuid = entry['publication_uuid']
        publication = Publication.objects.filter(uuid=pub_uuid).first()
        if publication is None:
            raise CommandError('No publication with uuid {0}'.format(pub_uuid))

        rows = self._rows_in_order(publication)
        before_rows = {row.uuid: snapshot(row) for row in rows}
        current_names = [row.author_name for row in rows]
        target = entry['authors']
        target_names = [item['name'] for item in target]
        fields = entry.get('fields', {})

        self.stdout.write(self.style.MIGRATE_LABEL(
            '{0}  {1}'.format(pub_uuid[:8], publication.title[:72])))
        if entry.get('note'):
            self.stdout.write('    {0}'.format(entry['note']))

        if self._already_applied(publication, rows, target, fields):
            self.stdout.write('    verified -- stored record already matches the paper')
            return False

        if current_names != entry['expect_authors']:
            raise CommandError(
                '{0}: stored authors have changed since the #62 survey and this entry has '
                'not been reviewed against them.\n  stored:   {1}\n  expected: {2}'.format(
                    pub_uuid[:8], current_names, entry['expect_authors']))

        current_attr = {row.fabric_uuid: i for i, row in enumerate(rows) if row.fabric_uuid}
        target_attr = {item['fabric_uuid']: j for j, item in enumerate(target)
                       if item.get('fabric_uuid')}

        dropped = set(current_attr) - set(target_attr)
        if dropped:
            raise CommandError(
                '{0}: the plan would leave {1} attributed to nothing. A repair never '
                'unclaims anyone.'.format(pub_uuid[:8], sorted(dropped)))
        # The mirror of that rule. This command moves attribution the publication already
        # carries; it does not decide new ones. Writing fabric_uuid here without a ledger
        # row would leave an attribution nothing in the table accounts for, and deciding
        # one belongs on the claim queue or the author edit form, where a human does it.
        invented = set(target_attr) - set(current_attr)
        if invented:
            raise CommandError(
                '{0}: the plan attributes {1}, who is not attributed on this publication '
                'today. Decide that on the claim queue; a repair moves attribution, it '
                'does not create it.'.format(pub_uuid[:8], sorted(invented)))

        moves = {f: (i, target_attr[f]) for f, i in current_attr.items()
                 if target_attr[f] != i}

        # 1. Detach every attribution that is changing slot, so the positional rename in
        #    _sync_authors cannot hand one person's claim to another.
        for fabric_uuid, (src, _dst) in moves.items():
            rows[src].fabric_uuid = None
            rows[src].save(update_fields=['fabric_uuid'])

        # 2. Carry the ledger rows across, for destinations that already exist. A
        #    destination past the current list length is created by the update below and
        #    is picked up in step 6.
        for fabric_uuid, (src, dst) in moves.items():
            if dst < len(rows):
                moved = relocate_claims(
                    source=rows[src], destination=rows[dst],
                    api_user=ApiUser.objects.get(uuid=fabric_uuid))
                self.stdout.write('    move   {0} slot {1} -> {2} ({3} claim row(s))'.format(
                    fabric_uuid[:8], src, dst, moved))

        # 3. Rows past the end of the target list are about to be deleted by
        #    _sync_authors, and that delete cascades to their claims. A decided claim is
        #    never collateral damage.
        for row in rows[len(target_names):]:
            if row.fabric_uuid or AuthorClaim.objects.filter(author=row).exclude(
                    status=AuthorClaim.SUGGESTED).exists():
                raise CommandError(
                    '{0}: surplus row "{1}" still carries attribution or a decided claim; '
                    'the plan must move it before the row is dropped.'.format(
                        pub_uuid[:8], row.author_name))
            withdrawn = withdraw_suggestions(row)
            self.stdout.write('    drop   slot {0} "{1}" ({2} suggestion(s) withdrawn)'.format(
                row.author_order, row.author_name, withdrawn))

        # 4. The app's own update path: names, author_order, surplus deletion and the
        #    publication fields, in one transaction. `authors` is always passed alongside
        #    `bibtex` so that the stored BibTeX cannot re-supply the broken author list
        #    parse_bibtex made of it in the first place.
        payload = dict(fields)
        payload['authors'] = target_names
        update_publication(
            publication=publication, data=payload, api_user=api_user,
            author_slots=[rows[i].uuid if i < len(rows) else None for i in range(len(target_names))],
            author_correction_reason=entry.get('note') or 'Apply reviewed author-record repair plan',
        )
        publication.refresh_from_db()
        new_rows = self._rows_in_order(publication)

        # 5. display_name is what the page renders, and _sync_authors does not touch it.
        #    A row still following its credited person's account name keeps it (#73); an
        #    uncredited row -- including one whose attribution step 1 just detached --
        #    goes back to its byline, whatever it showed before.
        for row in new_rows:
            if row.fabric_uuid and row.display_name_source != Author.BYLINE:
                continue
            if row.display_name != row.author_name or row.display_name_source != Author.BYLINE:
                row.display_name = row.author_name
                row.display_name_source = Author.BYLINE
                row.save(update_fields=['display_name', 'display_name_source'])

        # 6. Re-attach attribution, including onto rows created a moment ago.
        for fabric_uuid, slot in target_attr.items():
            row = new_rows[slot]
            if row.fabric_uuid != fabric_uuid:
                row.fabric_uuid = fabric_uuid
                row.save(update_fields=['fabric_uuid'])
            api = ApiUser.objects.get(uuid=fabric_uuid)
            stray = AuthorClaim.objects.filter(
                api_user=api, author__publication_uuid=pub_uuid
            ).exclude(author=row).exclude(status=AuthorClaim.SUGGESTED).first()
            if stray is not None:
                relocate_claims(source=stray.author, destination=row, api_user=api)

        for slot, (before, after) in enumerate(
                zip(current_names + [''] * len(target_names), target_names)):
            if before != after:
                self.stdout.write('    [{0}] {1!r} -> {2!r}'.format(slot, before, after))
        for name in sorted(fields):
            self.stdout.write('    field  {0} written'.format(name))

        self._verify(publication, target)
        after_rows = {row.uuid: row for row in self._rows_in_order(publication)}
        for original in rows:
            before = before_rows[original.uuid]
            after = after_rows.get(original.uuid)
            if after is None or snapshot(after) != before:
                record_correction(
                    after or original, api_user,
                    entry.get('note') or 'Apply reviewed author-record repair plan',
                    before, deleted=after is None,
                )
        return True

    def _already_applied(self, publication, rows, target, fields):
        """
        True when the stored record already is what the plan asks for, which is both the
        idempotency check and what makes a no-change entry read as a verification.
        """
        names = [item['name'] for item in target]
        if [row.author_name for row in rows] != names:
            return False
        if any(row.display_name != name for row, name in zip(rows, names)
               if row.display_name_source == Author.BYLINE):
            return False
        for slot, item in enumerate(target):
            if (rows[slot].fabric_uuid or None) != (item.get('fabric_uuid') or None):
                return False
        for name, value in fields.items():
            if getattr(publication, name, None) != value:
                return False
        return True

    def _verify(self, publication, target):
        rows = self._rows_in_order(publication)
        names = [row.author_name for row in rows]
        expected = [item['name'] for item in target]
        if names != expected:
            raise CommandError('post-check failed for {0}: {1} != {2}'.format(
                publication.uuid[:8], names, expected))
        if [row.author_order for row in rows] != list(range(len(rows))):
            raise CommandError('post-check failed for {0}: author_order is not 0..n'.format(
                publication.uuid[:8]))
        for slot, item in enumerate(target):
            want = item.get('fabric_uuid') or None
            if (rows[slot].fabric_uuid or None) != want:
                raise CommandError(
                    'post-check failed for {0}: slot {1} is attributed to {2}, expected '
                    '{3}'.format(publication.uuid[:8], slot, rows[slot].fabric_uuid, want))

    # -- orphan rows -------------------------------------------------------------

    def _delete_orphan(self, entry, api_user):
        """
        Delete an Author row that no publication's `authors` array names.

        #62 D asked us not to delete the one row in this class, on the grounds that it
        carries a real person's claim. It does -- and so does another row, for the same
        person, on the same publication, which *is* in the array. The row is a duplicate,
        not an unreachable attribution, and the preconditions below are what make that
        difference checkable rather than asserted: same publication, same name, same
        fabric_uuid, and every claim it holds also held by the row that survives.
        """
        author = Author.objects.filter(uuid=entry['author_uuid']).first()
        if author is None:
            self.stdout.write('    orphan {0} already gone'.format(entry['author_uuid'][:8]))
            return
        self.stdout.write(self.style.MIGRATE_LABEL(
            'orphan {0} "{1}"'.format(author.uuid[:8], author.author_name)))
        if Publication.objects.filter(authors__contains=[author.uuid]).exists():
            raise CommandError(
                '{0} is listed on a publication; it is not an orphan.'.format(
                    author.uuid[:8]))
        twin = Author.objects.filter(
            publication_uuid=author.publication_uuid,
            author_name=author.author_name,
            fabric_uuid=author.fabric_uuid,
        ).exclude(uuid=author.uuid).first()
        if twin is None or not Publication.objects.filter(
                uuid=author.publication_uuid, authors__contains=[twin.uuid]).exists():
            raise CommandError(
                '{0} has no listed twin on {1}; refusing to delete a row that carries the '
                'only copy of an attribution.'.format(
                    author.uuid[:8], author.publication_uuid[:8]))
        surviving = set(AuthorClaim.objects.filter(author=twin).values_list(
            'api_user__uuid', flat=True))
        losing = set(AuthorClaim.objects.filter(author=author).values_list(
            'api_user__uuid', flat=True))
        if losing - surviving:
            raise CommandError(
                '{0} holds claims for {1} that the surviving row does not.'.format(
                    author.uuid[:8], sorted(losing - surviving)))
        self.stdout.write('    duplicate of {0} (slot {1}); deleting {2} claim row(s)'.format(
            twin.uuid[:8], twin.author_order, len(losing)))
        record_correction(
            author, api_user, entry.get('note') or 'Remove reviewed duplicate orphan author',
            snapshot(author), deleted=True,
        )
        author.delete()

    # -- acceptance criteria -----------------------------------------------------

    def _report_criteria(self):
        """The #62 acceptance criteria, as queries rather than as prose."""
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('acceptance criteria'))

        bad_rows = []
        duplicate_names = []
        listed = set()
        for publication in Publication.objects.all():
            listed.update(publication.authors)
            rows = self._rows_in_order(publication)
            names = [row.author_name for row in rows]
            for row in rows:
                name = row.author_name
                if any(s in name for s in FORBIDDEN_SUBSTRINGS) \
                        or name.strip().lower() in FORBIDDEN_EXACT:
                    bad_rows.append((publication.uuid[:8], row.author_order, name))
            for name in sorted(set(names)):
                if names.count(name) > 1:
                    duplicate_names.append((publication.uuid[:8], name, names.count(name)))

        unreachable = list(Author.objects.exclude(uuid__in=listed).values_list(
            'uuid', 'author_name')[:10])
        no_bibtex = list(Publication.objects.filter(bibtex__isnull=True).values_list(
            'uuid', 'title')[:10])

        self._criterion('author rows containing ";", " and " or "others"', bad_rows)
        self._criterion('publications listing one author name twice', duplicate_names)
        self._criterion('Author rows in no publication authors array', unreachable)
        self._criterion('publications with no stored BibTeX', no_bibtex)

    def _criterion(self, label, offenders):
        if offenders:
            self.stdout.write(self.style.ERROR(
                '  FAIL  {0}: {1}'.format(label, len(offenders))))
            for offender in offenders[:10]:
                self.stdout.write('          {0}'.format(offender))
        else:
            self.stdout.write(self.style.SUCCESS('  ok    {0}: 0'.format(label)))
