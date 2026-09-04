"""
The one place an AuthorClaim decision is written (issue #32).

Three code paths decide claims and they must agree on what a decision *is*, because the
ledger is only worth keeping if every row means the same thing:

  * the admin queue approves or rejects a machine suggestion (views.author_claim_list),
  * the self-claim path records a `self_asserted` row (views.author_update),
  * the admin author form records an `approved` row for a hand-entered fabric_uuid
    (views.author_update).

`Author.fabric_uuid` stays the single authoritative field. It is written here only by
approve_suggestion(), and only after the guard below; the record_* functions are called
*after* the caller has already written it through its own existing path, and they only
add the ledger row that path never had. Scoring never writes it at all.

Two rules the whole module exists to keep consistent:

**Deciding an author withdraws its other standing suggestions.** Attribution is settled
once fabric_uuid is written, and leaving the other four candidates in the queue invites an
admin to approve a second one and silently overwrite the first. The next scoring run would
withdraw them anyway -- a claimed author is no longer scored, so its suggestions go stale
-- so this is that same withdrawal, made immediately instead of up to a day later.

**Withdrawal only ever touches `suggested` rows.** A decision is never removed by code:
rejections are the only record of what an admin has already ruled out, and they are what
stops a pair being re-suggested every night.
"""

from datetime import datetime, timezone
from uuid import uuid4

from django.db import transaction

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import AuthorClaim


class ClaimDecisionError(Exception):
    """A decision that must not be applied. The message is shown to the admin verbatim."""


def withdraw_suggestions(author, *, keep_api_user_id=None) -> int:
    """
    Delete this author's standing suggestions, optionally sparing one, and return how
    many went. Decided rows are never in scope.
    """
    queryset = AuthorClaim.objects.filter(author=author, status=AuthorClaim.SUGGESTED)
    if keep_api_user_id is not None:
        queryset = queryset.exclude(api_user_id=keep_api_user_id)
    return queryset.delete()[0]


def _record(author, api_user, *, status, source, decided_by, signals=None) -> AuthorClaim:
    """
    Create or update the (author, api_user) row. Keyed on the pair, which is what the
    unique constraint is on, so a self-claim of a pair the scorer had already suggested
    promotes that row rather than colliding with it.
    """
    claim, _ = AuthorClaim.objects.update_or_create(
        author=author,
        api_user=api_user,
        defaults={
            'status': status,
            'source': source,
            'decided_at': datetime.now(timezone.utc),
            'decided_by': decided_by,
        },
        # Only on insert: an existing row keeps the uuid it was created with, and keeps
        # the score and signals the scorer computed. Overwriting those would throw away
        # the only evidence of why the pair was ever suggested.
        create_defaults={
            'status': status,
            'source': source,
            'decided_at': datetime.now(timezone.utc),
            'decided_by': decided_by,
            'signals': signals or {},
            'uuid': str(uuid4()),
        },
    )
    return claim


@transaction.atomic
def approve_suggestion(claim, *, decided_by) -> int:
    """
    Approve a suggestion: write the attribution, stamp the decision, withdraw the rest.

    Returns the number of sibling suggestions withdrawn. Raises ClaimDecisionError when
    the approval must not be applied.
    """
    if claim.is_decided:
        raise ClaimDecisionError(
            'This claim was already {0} and cannot be decided again.'.format(claim.status)
        )
    author = claim.author
    # Refuse rather than overwrite. Between the scoring run and this click the author may
    # have been claimed -- by its owner through the self-claim path, or by another admin
    # in another tab -- and a scored suggestion is not grounds for taking an existing
    # attribution away from whoever holds it. Reassignment stays where it already is, on
    # the author edit form, where an admin is looking at the current value while changing
    # it.
    if author.fabric_uuid and author.fabric_uuid != claim.api_user.uuid:
        raise ClaimDecisionError(
            'Author "{0}" is already claimed by {1}. Approving this suggestion would '
            'overwrite that attribution; change it on the author edit page if it is '
            'wrong.'.format(author.author_name, author.fabric_uuid)
        )

    author.fabric_uuid = claim.api_user.uuid
    author.save(update_fields=['fabric_uuid'])

    claim.status = AuthorClaim.APPROVED
    claim.decided_at = datetime.now(timezone.utc)
    claim.decided_by = decided_by
    claim.save(update_fields=['status', 'decided_at', 'decided_by', 'modified'])

    return withdraw_suggestions(author, keep_api_user_id=claim.api_user_id)


@transaction.atomic
def reject_suggestion(claim, *, decided_by) -> None:
    """
    Reject a suggestion. Attribution is untouched, and the row is kept rather than
    deleted -- that is the whole point of a rejection.
    """
    if claim.is_decided:
        raise ClaimDecisionError(
            'This claim was already {0} and cannot be decided again.'.format(claim.status)
        )
    claim.status = AuthorClaim.REJECTED
    claim.decided_at = datetime.now(timezone.utc)
    claim.decided_by = decided_by
    claim.save(update_fields=['status', 'decided_at', 'decided_by', 'modified'])


@transaction.atomic
def record_self_claim(author, api_user) -> AuthorClaim:
    """
    Record the immediate self-claim path as `self_asserted` (option (a) on the issue).

    The claim itself has already been written by the caller; this is the ledger entry it
    never had. Migration 0003 backfilled the same row for every claim made before the
    ledger existed, so without this the ledger would start complete and immediately begin
    drifting again.
    """
    claim = _record(
        author, api_user,
        status=AuthorClaim.SELF_ASSERTED,
        source=AuthorClaim.SELF,
        # A live self-claim knows both, unlike the backfill, which left them NULL rather
        # than invent a date for a claim made at an unknown time by an unknown person.
        decided_by=api_user,
    )
    withdraw_suggestions(author, keep_api_user_id=api_user.id)
    return claim


@transaction.atomic
def record_admin_claim(author, fabric_uuid, *, decided_by) -> AuthorClaim | None:
    """
    Record an attribution an admin typed into the author edit form as `approved`.

    Returns None when there is nothing to record: a cleared fabric_uuid, or one naming no
    ApiUser. `Author.fabric_uuid` is a CharField rather than a foreign key, so it can hold
    a UUID the directory has never seen -- the same case migration 0003 counted and
    reported. It cannot become a claim, since the row needs an api_user, and the
    attribution the admin asked for is written regardless by the caller.

    Clearing an attribution deliberately leaves the previous decision in place. A claim
    that was made and later undone is history, and history is what this table is.
    """
    if not fabric_uuid:
        return None
    api_user = ApiUser.objects.filter(uuid=fabric_uuid).first()
    if api_user is None:
        return None
    claim = _record(
        author, api_user,
        status=AuthorClaim.APPROVED,
        # Not MACHINE: nothing suggested this pair, an admin asserted it. Telling the two
        # apart is what makes the approved rows usable for measuring how well the scorer
        # actually does -- hand-entered attributions would otherwise look like suggestions
        # it got right.
        source=AuthorClaim.ADMIN,
        decided_by=decided_by,
    )
    withdraw_suggestions(author, keep_api_user_id=api_user.id)
    return claim
