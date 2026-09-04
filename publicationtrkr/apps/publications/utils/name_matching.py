"""
Name compatibility scoring for author-claim suggestions (#32).

Publication author strings are ambiguous by nature -- `Smith, J.`, `J. Smith`,
`Jane Smith`, `Jane Q. Smith`, `Smith-Okonkwo, J.` -- which is exactly why claiming was
honor-system to begin with. The response here is to *score* the ambiguity and hand it to
an admin, never to decide it.

Two rules shape everything below:

  - **Surname agreement is necessary.** Different surnames means no candidate at all, not
    a low score. This is what keeps the queue finite: without it every author pairs with
    every one of 3,300 users.
  - **Absence is not disagreement.** A middle name on one side only, or an initial where
    the other side has a full given name, is missing information rather than evidence
    against. Penalising it would rank `J. Smith` below a total stranger.

Nicknames (Bob/Robert) and transliteration are deliberately not attempted. Guessing there
produces confident wrong answers, and the whole design is to feed uncertainty to a human.
"""

import re
import unicodedata

# Tokens that carry no identity and appear on one side or the other inconsistently.
SUFFIXES = frozenset({'jr', 'sr', 'ii', 'iii', 'iv', 'phd', 'md'})

# Surname particles. In `First M. Last` order these belong to the surname, and dropping
# one turns `Anna van der Berg` into a person surnamed `berg` with middle names
# `van der` -- who then fails to match `van der Berg, Anna` from the same paper's BibTeX.
PARTICLES = frozenset({
    'van', 'von', 'der', 'den', 'de', 'del', 'della', 'di', 'da', 'das', 'dos', 'du',
    'la', 'le', 'les', 'ter', 'ten', 'zu', 'af', 'av', 'al', 'bin', 'ibn', 'st', 'saint',
})

EXACT_GIVEN = 1.0
FULL_VS_INITIAL = 0.7
INITIAL_VS_INITIAL = 0.45
GIVEN_MISSING = 0.35
GIVEN_MISMATCH = 0.0


def fold(text: str) -> str:
    """Case-fold and strip diacritics, so `Muñoz` and `Munoz` compare equal."""
    if not text:
        return ''
    decomposed = unicodedata.normalize('NFKD', text)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold().strip()


def _clean_token(token: str) -> str:
    return re.sub(r'[^\w-]', '', fold(token))


def parse_name(raw: str) -> dict:
    """
    Reduce a name to {surname, given, middles} in a form the two sides can be compared in.

    Handles both orders: `Last, First M.` from BibTeX and `First M. Last` from the FABRIC
    directory. The comma is the only reliable signal of which one you have, so it decides.
    """
    if not raw:
        return {'surname': '', 'given': '', 'middles': []}

    text = fold(raw)
    # Strip a parenthesised alias, e.g. "Jane (Janie) Smith".
    text = re.sub(r'\([^)]*\)', ' ', text)

    if ',' in text:
        surname_part, _, given_part = text.partition(',')
    else:
        tokens = [t for t in re.split(r'\s+', text) if t]
        tokens = [t for t in tokens if _clean_token(t) not in SUFFIXES]
        if not tokens:
            return {'surname': '', 'given': '', 'middles': []}
        # Trailing token is the surname in `First M. Last` order. Keep particles with it
        # so `van der Berg` stays one surname rather than a middle name of `van der`.
        particles = PARTICLES
        cut = len(tokens) - 1
        while cut > 0 and _clean_token(tokens[cut - 1]) in particles:
            cut -= 1
        surname_part = ' '.join(tokens[cut:])
        given_part = ' '.join(tokens[:cut])

    surname = _clean_token(surname_part.replace(' ', '-')) if ' ' in surname_part.strip() \
        else _clean_token(surname_part)
    given_tokens = [_clean_token(t) for t in re.split(r'[\s.]+', given_part) if _clean_token(t)]
    given_tokens = [t for t in given_tokens if t not in SUFFIXES]

    return {
        'surname': surname,
        'given': given_tokens[0] if given_tokens else '',
        'middles': given_tokens[1:],
    }


def surnames_match(left: dict, right: dict) -> bool:
    """
    Surname agreement, tolerant of hyphenation only in the direction that is safe.

    `smith-okonkwo` matches `smith` (a married or double-barrelled name recorded one way
    in a paper and another in the directory), but `smith` does not match `okonkwo` --
    the component has to be a whole component, not a substring, or `son` would match
    `johnson`.
    """
    a, b = left['surname'], right['surname']
    if not a or not b:
        return False
    if a == b:
        return True
    a_parts, b_parts = set(a.split('-')), set(b.split('-'))
    return bool(a_parts & b_parts)


def given_name_score(left: dict, right: dict) -> tuple:
    """
    Score the given name, returning (score, explanation).

    The explanation is not decoration: it is what the admin queue shows, and a score
    without one is unreviewable.
    """
    a, b = left['given'], right['given']
    if not a or not b:
        return GIVEN_MISSING, 'given name absent on one side'

    a_initial, b_initial = len(a) == 1, len(b) == 1
    if not a_initial and not b_initial:
        if a == b:
            return EXACT_GIVEN, 'given names match exactly'
        return GIVEN_MISMATCH, 'given names differ ({0} vs {1})'.format(a, b)

    if a_initial and b_initial:
        if a == b:
            return INITIAL_VS_INITIAL, 'both sides give only the initial {0}.'.format(a)
        return GIVEN_MISMATCH, 'initials differ ({0}. vs {1}.)'.format(a, b)

    full, initial = (b, a) if a_initial else (a, b)
    if full.startswith(initial):
        return FULL_VS_INITIAL, '{0}. is consistent with {1}'.format(initial, full)
    return GIVEN_MISMATCH, '{0}. is not consistent with {1}'.format(initial, full)


def name_compatibility(author_name: str, user_name: str) -> tuple:
    """
    Compare two names, returning (score in 0..1, explanation, matched: bool).

    `matched` is the necessary condition -- surnames agree -- and is what the caller
    filters on. A False here means "not a candidate", not "a bad candidate".
    """
    left, right = parse_name(author_name), parse_name(user_name)
    if not surnames_match(left, right):
        return 0.0, 'surnames differ', False

    score, why = given_name_score(left, right)
    detail = 'surname {0} matches; {1}'.format(left['surname'] or right['surname'], why)
    return score, detail, True
