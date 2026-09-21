"""
Management command: sync_author_display_names

Brings credited authors' display names over to their FABRIC account names (#73), once.
After this, crediting, the directory sync and the login refresh keep them in step; this
command is the catch-up for rows that were credited before any of that existed, and it is
safe to run again.

**Dry run by default.** The preview lists every row whose displayed name would change,
grouped by what kind of change it is, and every row skipped with the reason. That list is
the review: on 2026-09-21, 57 of the 124 changes in production dropped a middle name or
initial that the byline printed. The rule says that is right -- it is the name the person
chose in FABRIC -- but it is a person's name on a published paper, so a human reads the
list before `--apply`.

What `--apply` writes, in one transaction:

  * credited rows whose account name is usable: `display_name` becomes that name and
    `display_name_source` becomes `account`, including rows that already show it, so a
    later change in the portal reaches them too;
  * nothing else. `custom` rows, rows whose account name is unusable (all lowercase,
    ALL CAPS, a single token, ...), rows credited to a uuid the directory does not know,
    and `byline` rows whose display_name was edited before provenance existed are all
    skipped and reported. `author_name` is never written.

Usage:
    python manage.py sync_author_display_names            # dry run, no writes
    python manage.py sync_author_display_names --apply    # atomic update of eligible rows
"""

from collections import Counter, defaultdict
from contextlib import nullcontext

from django.core.management.base import BaseCommand
from django.db import transaction

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import Author
from publicationtrkr.utils.names import check_account_name

CHANGE_KINDS = (
    'drops a name part',
    'adds a name part',
    'initial -> full name',
    'full name -> initial',
    'different given-name form',
    'word order',
    'different surname',
    'case or punctuation only',
)


def _tokens(name):
    return [token.strip('.,').lower() for token in name.split()]


def change_kind(old, new) -> str:
    """One label per change, for grouping the preview. A reading aid, not a rule."""
    before, after = _tokens(old), _tokens(new)
    if before == after:
        return 'case or punctuation only'
    if set(after) < set(before):
        return 'drops a name part'
    if set(before) < set(after):
        return 'adds a name part'
    if len(before) == len(after):
        if all(a == b or (len(a) == 1 and b.startswith(a)) for a, b in zip(before, after)):
            return 'initial -> full name'
        if all(a == b or (len(b) == 1 and a.startswith(b)) for a, b in zip(before, after)):
            return 'full name -> initial'
    if before[-1:] == after[-1:]:
        return 'different given-name form'
    if sorted(before) == sorted(after):
        return 'word order'
    return 'different surname'


class Command(BaseCommand):
    help = ("Preview credited authors' display names following their FABRIC account names; "
            'use --apply to save. author_name and attribution stay unchanged.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Update eligible credited rows atomically.')

    def handle(self, *args, **options):
        apply = options['apply']
        identical, followed, changes, skipped = [], [], [], []
        uninverted = Counter()
        with transaction.atomic() if apply else nullcontext():
            rows = Author.objects.exclude(fabric_uuid__isnull=True).exclude(fabric_uuid='').order_by('pk')
            if apply:
                # People first, then their Author rows, each in primary-key order: the order
                # a name change takes (display_names). A name change or a post-credit
                # recheck arriving meanwhile waits, then sees what this wrote.
                list(ApiUser.objects.select_for_update(no_key=True)
                     .filter(uuid__in=rows.values('fabric_uuid')).order_by('pk').values_list('pk'))
                rows = rows.select_for_update()
            rows = list(rows)
            names = dict(ApiUser.objects.filter(uuid__in={row.fabric_uuid for row in rows})
                         .values_list('uuid', 'name'))
            for row in rows:
                if row.display_name_source == Author.CUSTOM:
                    skipped.append((row, 'custom name', None))
                    continue
                if row.fabric_uuid not in names:
                    skipped.append((row, 'no FABRIC account with this uuid', None))
                    continue
                raw = names[row.fabric_uuid]
                account_name, reason = check_account_name(raw)
                if account_name is None:
                    skipped.append((row, 'account name unusable: ' + reason, raw))
                    continue
                if row.display_name_source == Author.BYLINE and row.display_name != row.author_name:
                    skipped.append((row, 'display_name edited before provenance was recorded', raw))
                    continue
                if ',' in raw and ',' not in account_name:
                    uninverted['identical' if row.display_name == account_name else 'changing'] += 1
                if row.display_name == account_name:
                    (followed if row.display_name_source == Author.ACCOUNT else identical).append(row)
                else:
                    changes.append((row, account_name, change_kind(row.display_name, account_name)))

            if apply:
                for row in identical:
                    row.display_name_source = Author.ACCOUNT
                for row, account_name, _ in changes:
                    row.display_name = account_name
                    row.display_name_source = Author.ACCOUNT
                Author.objects.bulk_update(
                    identical + [row for row, _, _ in changes], ['display_name', 'display_name_source'])

        self._report(apply, rows, identical, followed, changes, skipped, uninverted)

    def _report(self, apply, rows, identical, followed, changes, skipped, uninverted):
        out = self.stdout.write
        out('APPLY' if apply else 'DRY RUN — use --apply to save changes.')

        by_kind = defaultdict(list)
        for change in changes:
            by_kind[change[2]].append(change)
        for kind in CHANGE_KINDS:
            if by_kind[kind]:
                out('\n{0} ({1}):'.format(kind, len(by_kind[kind])))
                for row, account_name, _ in sorted(by_kind[kind], key=lambda c: (c[1], c[0].pk)):
                    out('  {0}  {1!r} -> {2!r}  (byline {3!r}, publication {4})'.format(
                        row.uuid, row.display_name, account_name, row.author_name, row.publication_uuid))
        if skipped:
            out('\nskipped ({0}):'.format(len(skipped)))
            for row, reason, raw in sorted(skipped, key=lambda s: (s[1], s[0].pk)):
                out('  {0}  {1!r}  {2}{3}'.format(
                    row.uuid, row.display_name, reason, '' if raw is None else ' ({0!r})'.format(raw)))

        verb = 'Changed' if apply else 'Would change'
        out('\n--- Summary ---')
        out('Credited rows : {0} ({1} people)'.format(len(rows), len({row.fabric_uuid for row in rows})))
        out('Identical     : {0} ({1} already following; {2} {3} as following)'.format(
            len(identical) + len(followed), len(followed), len(identical),
            'marked' if apply else 'to be marked'))
        out('{0:<14}: {1}'.format(verb, len(changes)))
        for kind in CHANGE_KINDS:
            if by_kind[kind]:
                out('  {0:<26}: {1}'.format(kind, len(by_kind[kind])))
        out('Skipped       : {0}'.format(len(skipped)))
        for reason, count in sorted(Counter(reason for _, reason, _ in skipped).items()):
            out('  {0:<26}: {1}'.format(reason, count))
        out('Un-inverted   : {0} row(s) from "Surname, Given" ({1} identical once un-inverted, '
            '{2} changing)'.format(sum(uninverted.values()), uninverted['identical'], uninverted['changing']))
