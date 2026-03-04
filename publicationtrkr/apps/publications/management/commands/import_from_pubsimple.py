"""
Management command: import_from_pubsimple

Copies all PubSimple records into the Publication/Author model, preserving:
  - created / modified timestamps
  - created_by / modified_by FK references to ApiUser

Each PubSimple author string becomes a new Author object linked to the
new Publication via Author.publication_uuid and tracked in Publication.authors
(the array of Author UUIDs).

Records are skipped when a Publication with the same (title, link) already
exists, matching the unique constraints on the Publication model.

Usage:
    python manage.py import_from_pubsimple            # live run
    python manage.py import_from_pubsimple --dry-run  # preview only
"""

from uuid import uuid4

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from publicationtrkr.apps.pubsimple.models import PubSimple
from publicationtrkr.apps.publications.models import Author, Publication


class Command(BaseCommand):
    help = 'Import PubSimple records into the Publication/Author models.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Preview what would be imported without writing to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be written.\n'))

        pubsimples = PubSimple.objects.select_related('created_by', 'modified_by').all()
        total = pubsimples.count()
        self.stdout.write(f'PubSimple records found: {total}\n')

        imported = 0
        skipped = 0
        errors = 0

        for ps in pubsimples:
            # Determine whether a conflicting Publication already exists,
            # matching the same unique constraints used on the Publication model.
            if ps.link is None:
                conflict = Publication.objects.filter(title=ps.title, link__isnull=True).exists()
            else:
                conflict = Publication.objects.filter(title=ps.title, link=ps.link).exists()

            if conflict:
                self.stdout.write(
                    f'  SKIP  (already exists) "{ps.title[:80]}"'
                )
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(
                    f'  WOULD IMPORT  "{ps.title[:80]}"  '
                    f'({len(ps.authors)} author(s))'
                )
                imported += 1
                continue

            try:
                with transaction.atomic():
                    pub_uuid = str(uuid4())

                    # Create Author objects for each name in PubSimple.authors
                    author_uuids = []
                    for author_name in ps.authors:
                        author = Author(
                            author_name=author_name,
                            display_name=author_name,
                            fabric_uuid=None,
                            publication_uuid=pub_uuid,
                            uuid=str(uuid4()),
                        )
                        author.save()
                        author_uuids.append(author.uuid)

                    # Create the Publication (created/modified set by auto_now*)
                    pub = Publication(
                        authors=author_uuids,
                        bibtex=None,
                        created_by=ps.created_by,
                        link=ps.link,
                        modified_by=ps.modified_by,
                        project_name=ps.project_name,
                        project_uuid=ps.project_uuid,
                        title=ps.title,
                        uuid=pub_uuid,
                        venue=ps.venue,
                        year=ps.year,
                    )
                    pub.save()

                    # Patch timestamps to match the original PubSimple values,
                    # bypassing auto_now_add / auto_now via queryset.update().
                    Publication.objects.filter(pk=pub.pk).update(
                        created=ps.created,
                        modified=ps.modified,
                    )

                self.stdout.write(
                    self.style.SUCCESS(
                        f'  IMPORTED  "{ps.title[:80]}"  '
                        f'({len(author_uuids)} author(s))'
                    )
                )
                imported += 1

            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(
                        f'  ERROR  "{ps.title[:80]}"  —  {exc}'
                    )
                )
                errors += 1

        self.stdout.write('\n--- Summary ---')
        if dry_run:
            self.stdout.write(self.style.WARNING(f'Would import : {imported}'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Imported     : {imported}'))
        self.stdout.write(f'Skipped      : {skipped}')
        if errors:
            self.stdout.write(self.style.ERROR(f'Errors       : {errors}'))
        else:
            self.stdout.write(f'Errors       : {errors}')
