"""
Score candidate (Author, ApiUser) pairs for the admin claim queue (#32).

Phase 1 (v1.14.0) uses only local signals -- nothing here calls out to Core API, so a
scoring run costs one pass over unclaimed authors and no network at all:

  1. **Project co-membership.** `Publication.project_uuid` against `ApiUser.projects`.
     The strongest signal available and free: a paper attributed to a project the person
     belongs to is real evidence, and it got substantially better when the directory went
     from 14 rows to 3,296 in 1.12.0.
  2. **Name compatibility.** Scored rather than matched -- see utils/name_matching.py.

Affiliation is dropped from phase 1: neither `Author` nor `Publication` carries an
affiliation to compare `ApiUser.affiliation` against, so it could only be invented.
Scholar/Scopus IDs and co-authorship propagation are phase 2 (v1.15.0).

**The score orders a queue and never approves anything.** That is the whole design, and
it is why the weights below can be tuned from real queue data later without any migration
or re-approval: a change here re-ranks suggestions and touches no decided row.

Surname agreement is a *gate*, not a weight. Without it every author would pair with
every one of 3,300 users and the queue would be 650,000 rows of noise instead of a
reviewable list.
"""

from publicationtrkr.apps.publications.utils.name_matching import name_compatibility

# Weights sum to 1.0, so a score is directly readable as a confidence in 0..1.
WEIGHT_PROJECT = 0.55
WEIGHT_NAME = 0.45

# Below this, a suggestion is noise rather than a candidate: a bare surname match with an
# outright given-name conflict scores 0.55 at most and only when the project also matches.
# Tunable from the first real queue -- that is what the fabric-dev backfill is for.
MIN_SCORE = 0.30


def project_signal(publication, api_user) -> tuple:
    """(value in 0..1, detail). Absence of a project on the paper is no signal, not a negative."""
    project_uuid = getattr(publication, 'project_uuid', None) if publication else None
    if not project_uuid:
        return 0.0, 'publication has no project'
    if project_uuid in (api_user.projects or []):
        return 1.0, 'member of the publication project {0}'.format(project_uuid)
    return 0.0, 'not a member of the publication project'


def score_pair(author, api_user, publication) -> dict | None:
    """
    Score one (author, user) pair, or return None when the pair is not a candidate.

    Returns {'score': float, 'signals': {...}} where `signals` is the per-signal
    breakdown the queue displays. An admin has to be able to see *why* something ranked
    where it did; a bare number is unreviewable.
    """
    name_value, name_detail, surname_matched = name_compatibility(
        author.author_name, api_user.name
    )
    if not surname_matched:
        return None

    project_value, project_detail = project_signal(publication, api_user)

    signals = {
        'project': {
            'weight': WEIGHT_PROJECT,
            'value': project_value,
            'detail': project_detail,
        },
        'name': {
            'weight': WEIGHT_NAME,
            'value': name_value,
            'detail': name_detail,
        },
    }
    score = sum(s['weight'] * s['value'] for s in signals.values())
    return {'score': round(score, 4), 'signals': signals}


def candidates_for_author(author, api_users, publication) -> list:
    """
    Every scoring candidate for one author, best first.

    Pairs below MIN_SCORE are dropped rather than stored: a suggestion nobody would ever
    approve still costs an admin the time to read it.
    """
    scored = []
    for api_user in api_users:
        result = score_pair(author, api_user, publication)
        if result is None or result['score'] < MIN_SCORE:
            continue
        scored.append((result['score'], api_user, result['signals']))
    scored.sort(key=lambda row: (-row[0], row[1].uuid))
    return scored
