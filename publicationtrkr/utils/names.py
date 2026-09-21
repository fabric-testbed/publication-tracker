"""Display-name normalization shared by directory sync and authentication."""

import re

from publicationtrkr.apps.publications.utils.bibtex_utils import normalize_author_name

# Generational suffixes. "Given Family, Jr." is written in reading order already, so the
# comma is not an inversion, and un-inverting it would print "Jr. Given Family".
_SUFFIXES = frozenset({'jr', 'sr', 'ii', 'iii', 'iv'})

# Surname particles that are written in lowercase on purpose ("Cees de Laat", "Maria dos
# Santos"), plus an elided particle glued to its surname ("Jean d'Alembert").
_PARTICLES = frozenset({
    'al', 'bin', 'binti', 'da', 'das', 'de', 'del', 'della', 'der', 'di', 'do', 'dos', 'du',
    'e', 'el', 'ibn', 'la', 'le', 'ten', 'ter', 'van', 'von', 'y', 'zu',
})
_ELIDED_PARTICLE = re.compile(r"^(d|l|dell)['\u2019]\w")


def normalize_person_name(value: str | None) -> str:
    """Collapse whitespace without changing spelling or using names as identity keys."""
    return ' '.join((value or '').split())


def check_account_name(value: str | None) -> tuple[str | None, str | None]:
    """
    Return `(name, None)` when an account name can stand on a paper, else `(None, reason)`.

    Account names are what people typed into COmanage or the FABRIC portal. Measured
    across all 3,346 of them on 2026-09-21: 80 are all-lowercase or ALL-CAPS, 22 are a
    single token, and 12 contain a comma, most of them inverted "Surname, Given". Copied
    as they are onto credited authors (#73), those put "Mahmud, Imtiaz" or "yulong xiao"
    on published papers where the byline had it right.

    So a name is un-inverted when it is inverted and otherwise *used as written or not at
    all*. Nothing here title-cases or "repairs" a name: `de Laat`, `McDonald` and
    `O'Neil` have no rule that fixes them all, and a guess that is wrong on someone's own
    name is worse than leaving their byline in place. An unusable name is skipped, and
    the reason says why.

    Casing is judged per word, not only for the whole name. The rehearsal preview against
    production found "Given SURNAME" and "Given middle Surname lowercase" forms that pass
    a whole-name check and would still put a shouted or half-lowercased name on a paper.
    A word of three or more letters in capitals (a roman-numeral suffix aside), or a
    lowercase word that is not a surname particle, makes the name unusable.

    The un-inversion is the #66 helper, `bibtex_utils.normalize_author_name`, which
    understands `von` parts. Names it cannot read with confidence are skipped rather than
    guessed at: more than one comma ("Surname, Given, Ph.D.") or a parenthetical note
    ("Surname, Given (Dept-Student)"), both of which it would scramble.
    """
    name = normalize_person_name(value)
    if not name:
        return None, 'empty'
    if '(' in name or ')' in name:
        return None, 'parenthetical note'
    parts = name.split(',')
    if len(parts) > 2:
        return None, 'several commas'
    if len(parts) == 2 and parts[1].strip().rstrip('.').lower() not in _SUFFIXES:
        name = normalize_author_name(name)
    if len(name.split()) < 2:
        return None, 'single name'
    if name.islower():
        return None, 'all lowercase'
    if name.isupper():
        return None, 'all capitals'
    for word in name.split():
        letters = word.strip('.,').replace('-', '').replace("'", '')
        if len(letters) >= 3 and letters.isalpha() and letters.isupper() \
                and letters.lower() not in _SUFFIXES:
            return None, 'a word in capitals'
        if word[0].islower() and word not in _PARTICLES and not _ELIDED_PARTICLE.match(word):
            return None, 'a word in lowercase'
    return name, None


def usable_account_name(value: str | None) -> str | None:
    """The account name as it should appear on a paper, or None when it is unusable."""
    return check_account_name(value)[0]
