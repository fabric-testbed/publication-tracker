"""
Which name an Author row shows, and why (#73).

A credited author's `display_name` follows that person's FABRIC account name when the
name is usable (`names.check_account_name`). `Author.display_name_source` records which
of three things the row shows, and every path that changes one of them goes through here
so the three stay true:

  * `byline`  -- a copy of `author_name`, the name as printed on the paper;
  * `account` -- the credited person's account name, kept in step when it changes;
  * `custom`  -- a name somebody chose, which nothing automatic overwrites.

`author_name` is never written here. Callers own the transaction and the locks: these
functions set fields on an Author they already hold, or, for an account-name change,
lock the affected rows themselves in primary-key order -- the order every other author
writer uses.

Two writers meet on one row here and they lock in different places, so the order between
them is spelled out. A name change holds the person's ApiUser row (the sync's UPDATE, or
save_refreshed_user) and then locks their `account` Author rows. Crediting holds the
publication and Author rows (author_mutations, claim_ledger) and reads the name without
locking the person. It cannot take that lock without either deadlocking against the name
change or reordering v1.20.0's publication-then-author locks, so it does not. That leaves
one race: a name change that commits between the credit's read and its commit does not
see the row as credited yet, and the paper keeps the old name. So a write that leaves a
row `account` calls recheck_after_commit(), which runs once it has committed and re-reads
the name in the name change's own order -- person first, then author.
"""

from django.db import transaction

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.apps.publications.models import Author
from publicationtrkr.utils.names import usable_account_name


def account_name_for(fabric_uuid) -> str | None:
    """The usable account name of the person `fabric_uuid` names, or None."""
    if not fabric_uuid:
        return None
    name = ApiUser.objects.filter(uuid=fabric_uuid).values_list('name', flat=True).first()
    return usable_account_name(name)


def automatic_display_name(author, account_name) -> tuple[str, str]:
    """What a row shows when nobody has chosen its name: the account name, else the byline."""
    if author.fabric_uuid and account_name:
        return account_name, Author.ACCOUNT
    return author.author_name, Author.BYLINE


def restore_byline(author) -> None:
    author.display_name = author.author_name
    author.display_name_source = Author.BYLINE


def use_automatic(author, account_name) -> None:
    """Return a row to automatic naming, the "use my FABRIC name" choice."""
    author.display_name, author.display_name_source = automatic_display_name(author, account_name)


def choose_display_name(author, display_name, account_name) -> None:
    """
    Record a name someone chose explicitly.

    A choice equal to what the row would show anyway is not a pin: choosing your own
    account name keeps the row following it, and an admin typing the byline back onto an
    uncredited row returns it to `byline`. Anything else is `custom`.
    """
    automatic, source = automatic_display_name(author, account_name)
    author.display_name = display_name
    author.display_name_source = source if display_name == automatic else Author.CUSTOM


def follow_account_name(author, account_name) -> bool:
    """
    Apply the crediting rule to an author that was just credited; True if anything changed.

    A `custom` name is kept. Otherwise a usable account name replaces what the row shows.
    An unusable one leaves the row as it is: the byline is better than "yulong xiao".
    """
    if author.display_name_source == Author.CUSTOM or not author.fabric_uuid or not account_name:
        return False
    if author.display_name == account_name and author.display_name_source == Author.ACCOUNT:
        return False
    author.display_name = account_name
    author.display_name_source = Author.ACCOUNT
    return True


def recheck_after_commit(author_pk) -> None:
    """Re-read an `account` row's name once the current transaction commits (see module doc)."""
    transaction.on_commit(lambda: _recheck(author_pk), robust=True)


@transaction.atomic
def _recheck(author_pk) -> None:
    fabric_uuid = Author.objects.filter(pk=author_pk).values_list('fabric_uuid', flat=True).first()
    if not fabric_uuid:
        return
    # FOR NO KEY UPDATE, like the UPDATE a name change takes: it waits for one in flight,
    # but not for the KEY SHARE locks that claim inserts take on the same row.
    name = ApiUser.objects.select_for_update(no_key=True).filter(uuid=fabric_uuid) \
        .values_list('name', flat=True).first()
    author = Author.objects.select_for_update().filter(
        pk=author_pk, fabric_uuid=fabric_uuid, display_name_source=Author.ACCOUNT).first()
    if author is not None and follow_account_name(author, usable_account_name(name)):
        author.save(update_fields=['display_name', 'display_name_source'])


@transaction.atomic
def follow_account_name_change(fabric_uuid, name, *, apply=True) -> tuple[int, int]:
    """
    Carry a changed account name onto that person's `account` rows.

    Returns `(changed, kept)`: rows whose display_name moved (or would, when `apply` is
    False), and rows left alone because the new name is unusable. `byline` and `custom`
    rows are never touched -- the first were never following the account, and the second
    were chosen by someone.

    The caller has already saved (or is about to save) the new name on the ApiUser row;
    the directory sync and the login refresh both hold that row's lock when they call
    this, so two changes to one person's name cannot interleave here.
    """
    rows = Author.objects.filter(fabric_uuid=fabric_uuid, display_name_source=Author.ACCOUNT)
    account_name = usable_account_name(name)
    if account_name is None:
        return 0, rows.count()
    if not apply:
        return rows.exclude(display_name=account_name).count(), 0
    stale = [row.pk for row in rows.select_for_update().order_by('pk') if row.display_name != account_name]
    Author.objects.filter(pk__in=stale).update(display_name=account_name)
    return len(stale), 0
