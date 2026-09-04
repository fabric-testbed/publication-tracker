"""
Score candidate (Author, ApiUser) pairs for the admin claim queue (#32).

Nothing here calls out to Core API. A scoring run costs one pass over unclaimed authors
and no network at all -- the one signal that needs external data reads a column the
nightly `sync_fabric_users` has already written onto a row this module was handed:

  1. **Project co-membership.** `Publication.project_uuid` against `ApiUser.projects`.
     The strongest signal available and free: a paper attributed to a project the person
     belongs to is real evidence, and it got substantially better when the directory went
     from 14 rows to 3,296 in 1.12.0.
  2. **Name compatibility.** Scored rather than matched -- see utils/name_matching.py.
  3. **Co-authorship propagation.** Whether this person is already attributed on a
     publication that shares a person with this one. Added in v1.15.0.
  4. **Scholar/Scopus identifier presence.** A weak prior, nothing more. Added in v1.15.0.

Affiliation is dropped rather than deferred: neither `Author` nor `Publication` carries an
affiliation to compare `ApiUser.affiliation` against, so it could only be invented.

**The score orders a queue and never approves anything.** That is the whole design, and
it is why the weights below can be tuned from real queue data later without any migration
or re-approval: a change here re-ranks suggestions and touches no decided row.

Surname agreement is a *gate*, not a weight. Without it every author would pair with
every one of 3,300 users and the queue would be 650,000 rows of noise instead of a
reviewable list. Neither v1.15.0 signal bypasses that gate, and that is deliberate:
adjacency in the co-authorship graph carries no information about *which* author string
on a paper a person is, so a propagation-driven candidate pass would confidently propose
that `Papadimitriou, G.` is Jane Smith. Propagation re-ranks candidates the scorer has
already evaluated; it never generates one.

Renormalising the weights (v1.15.0)
-----------------------------------
Adding weights to a vector that summed to 1.0 rescales every score already stored, and
`score_author_claims` deletes rows that stop scoring. v1.14.0's real queue had six
suggestions at 0.315 -- `E. Kfoury` -> `Elie Kfoury` and two like it, correct
suggestions, not noise -- sitting 0.015 above the old `MIN_SCORE` of 0.30. A naive
renormalisation would have deleted them.

So `MIN_SCORE` is scaled by exactly the factor the pre-existing weights are, `k = 0.8`:
0.55 -> 0.44, 0.45 -> 0.36, 0.30 -> 0.24. A pair that gains neither new signal therefore
keeps precisely its old membership of the queue -- 0.315 becomes 0.252, still above
0.24 -- and only pairs that gain a new signal move.

The second rule is `WEIGHT_PROPAGATION < MIN_SCORE`, asserted in the tests. Propagation
must never lift a pair over the threshold on its own, or a bare surname match qualifies
on graph proximity alone. That bound is what contains the compounding failure the issue
identifies as the one to design against: approve -> propagate -> suggest -> approve, with
the admin as the only damper. A wrongly approved `Silva` propagates to every `Silva` on
every adjacent paper, and at 0.15 those pairs can be reordered but never qualified.
"""

from publicationtrkr.apps.publications.utils.name_matching import name_compatibility

# Weights sum to 1.0, so a score is directly readable as a confidence in 0..1.
# The first two are v1.14.0's 0.55/0.45 scaled by k = 0.8 -- see the module docstring for
# why that factor, and why MIN_SCORE carries it too.
WEIGHT_PROJECT = 0.44
WEIGHT_NAME = 0.36
# Small on purpose. With 579 attributions across 191 publications, propagation fires
# broadly, and it says the same thing about every candidate standing in the same
# neighbourhood -- it floats whole publications up the queue rather than discriminating
# between candidates for one author. A broad, undiscriminating signal earns a small
# weight, not a large one.
WEIGHT_PROPAGATION = 0.15
# Weakest of the four, and honestly so: core-api had a Scholar identifier for 8 of 3,311
# people and a Scopus identifier for none. Presence is a prior on top of the name signal,
# never evidence on its own.
WEIGHT_SCHOLAR = 0.05

# Below this, a suggestion is noise rather than a candidate. Scaled with the weights above
# so that v1.14.0's queue membership is preserved exactly where no new signal fires.
MIN_SCORE = 0.24


def project_signal(publication, api_user) -> tuple:
    """(value in 0..1, detail). Absence of a project on the paper is no signal, not a negative."""
    project_uuid = getattr(publication, 'project_uuid', None) if publication else None
    if not project_uuid:
        return 0.0, 'publication has no project'
    if project_uuid in (api_user.projects or []):
        return 1.0, 'member of the publication project {0}'.format(project_uuid)
    return 0.0, 'not a member of the publication project'


def scholar_signal(api_user) -> tuple:
    """
    (value in 0..1, detail). Reads *presence* of a Scholar or Scopus identifier only.

    Deliberately not resolved to a publication list. Fetching one needs an Elsevier key
    for Scopus and has no official API at all for Scholar, with its own rate limits and
    failure modes, to serve the eight people who have an identifier -- so that is a
    separate issue, and nothing here is wasted if it is ever taken up: same request, same
    columns, same sync step.

    Absence is no signal rather than a negative, which matters more here than anywhere
    else in the module: almost nobody has one, so reading absence as evidence against
    would penalise 3,303 people out of 3,311 over a field they have never been asked to
    fill in.
    """
    held = [
        label for label, value in (
            ('Google Scholar', getattr(api_user, 'google_scholar', '') or ''),
            ('Scopus', getattr(api_user, 'scopus', '') or ''),
        )
        if value.strip()
    ]
    if not held:
        return 0.0, 'no Google Scholar or Scopus identifier on record'
    return 1.0, 'has a {0} identifier on record'.format(' and '.join(held))


def build_coauthorship_graph(attributions, *, publication_labels=None,
                             person_labels=None) -> dict:
    """
    Precompute, once per run, who is reachable from each publication at depth 1.

    `attributions` is an iterable of `(person_uuid, publication_uuid)` pairs -- every
    `Author` row carrying a `fabric_uuid`. Returns
    `{publication_uuid: {person_uuid: detail}}`: the people attributed on some *other*
    publication that shares at least one person with this one, each with the
    human-readable reason the boost exists.

    **Sourced from `Author.fabric_uuid`, not from `AuthorClaim.status == 'approved'`.**
    The issue specifies approved claims, and the intent behind that -- exclude machine
    guesses -- is preserved here, because a `suggested` row carries no `fabric_uuid` at
    all. But the status field is the wrong source in fact: production holds 579
    `self_asserted` rows, 77 `suggested` ones and zero `approved` ones, so the specified
    source computes an empty graph and contributes nothing, forever. The three write paths
    also record three different statuses for the same act of attribution, which makes the
    ledger's status an arbitrary partition of it. `fabric_uuid` is the field the rest of
    the codebase calls authoritative. The cost of this choice is that the 579 honor-system
    self-claims, which nobody verified, are admitted as sources -- which is a stated
    reason for `WEIGHT_PROPAGATION` being small and bounded below `MIN_SCORE`.

    **Adjacency is identity through `fabric_uuid`, never author-name equality.** Two
    publications share a *person*, not an `Author` row; matching names across publications
    would reintroduce exactly the `Smith, J.` / `J. Smith` ambiguity this system exists to
    escape.

    **Depth 1 only.** On a 191-publication graph, depth 2 is close to "is in the corpus",
    and any decay factor would be a number invented rather than measured.

    A person already attributed on the publication itself is excluded from its reach. They
    hold one of its author slots, so proposing them for a second slot on the same paper
    would be proposing that one person is two of its authors -- and the degenerate path
    that produces it is that person's own attribution, reflected back through themselves.
    """
    publication_labels = publication_labels or {}
    person_labels = person_labels or {}

    people_by_publication = {}
    publications_by_person = {}
    for person_uuid, publication_uuid in attributions:
        if not person_uuid or not publication_uuid:
            continue
        people_by_publication.setdefault(publication_uuid, set()).add(person_uuid)
        publications_by_person.setdefault(person_uuid, set()).add(publication_uuid)

    graph = {}
    for publication_uuid, people_here in people_by_publication.items():
        reach = {}
        # Sorted, not raw set iteration. A person can be reachable through several
        # co-authors and several publications, and only the first one found is recorded
        # as the witness in `detail`. Set iteration order over strings varies between
        # processes, so unsorted traversal picks a different witness on each nightly run;
        # the score is identical but the signals dict compares unequal, and
        # score_author_claims rewrites the row -- every night, forever. The rehearsal
        # caught exactly that: 12 rows "updated" with 0 raised and 0 lowered.
        for shared_person in sorted(people_here):
            for neighbour in sorted(publications_by_person[shared_person]):
                if neighbour == publication_uuid:
                    continue
                for person in sorted(people_by_publication[neighbour]):
                    if person in people_here or person in reach:
                        continue
                    reach[person] = (
                        'also attributed on "{0}", which shares co-author {1} with this '
                        'publication'.format(
                            publication_labels.get(neighbour, neighbour),
                            person_labels.get(shared_person, shared_person),
                        )
                    )
        if reach:
            graph[publication_uuid] = reach
    return graph


def propagation_signal(api_user, publication, graph) -> tuple:
    """
    (value in 0..1, detail). Whether this person is attributed on an adjacent publication.

    Binary rather than graded. A count of shared neighbours would read as strength of
    evidence and it is not: one shared co-author on a five-author paper and four on a
    fifty-author one say close to the same thing about whether *this* author string is
    *this* person.
    """
    if not graph or publication is None:
        return 0.0, 'no co-authorship neighbourhood for this publication'
    reach = graph.get(getattr(publication, 'uuid', None))
    if not reach:
        return 0.0, 'no co-authorship neighbourhood for this publication'
    detail = reach.get(getattr(api_user, 'uuid', None))
    if not detail:
        return 0.0, 'not attributed on any adjacent publication'
    return 1.0, detail


def score_pair(author, api_user, publication, graph=None) -> dict | None:
    """
    Score one (author, user) pair, or return None when the pair is not a candidate.

    Returns {'score': float, 'signals': {...}} where `signals` is the per-signal
    breakdown the queue displays. An admin has to be able to see *why* something ranked
    where it did; a bare number is unreviewable. For propagation that breakdown is
    load-bearing rather than decorative: once a suggestion has been approved, nothing
    distinguishes a laundered machine inference from ground truth unless the admin could
    see which publication and which co-author the boost travelled through.

    `graph` is what build_coauthorship_graph returned, or None -- the propagation signal
    then contributes 0.0 and the pair scores on the other three.
    """
    name_value, name_detail, surname_matched = name_compatibility(
        author.author_name, api_user.name
    )
    if not surname_matched:
        return None

    project_value, project_detail = project_signal(publication, api_user)
    propagation_value, propagation_detail = propagation_signal(
        api_user, publication, graph
    )
    scholar_value, scholar_detail = scholar_signal(api_user)

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
        'propagation': {
            'weight': WEIGHT_PROPAGATION,
            'value': propagation_value,
            'detail': propagation_detail,
        },
        'scholar': {
            'weight': WEIGHT_SCHOLAR,
            'value': scholar_value,
            'detail': scholar_detail,
        },
    }
    score = sum(s['weight'] * s['value'] for s in signals.values())
    return {'score': round(score, 4), 'signals': signals}


def candidates_for_author(author, api_users, publication, graph=None) -> list:
    """
    Every scoring candidate for one author, best first.

    Pairs below MIN_SCORE are dropped rather than stored: a suggestion nobody would ever
    approve still costs an admin the time to read it.
    """
    scored = []
    for api_user in api_users:
        result = score_pair(author, api_user, publication, graph)
        if result is None or result['score'] < MIN_SCORE:
            continue
        scored.append((result['score'], api_user, result['signals']))
    scored.sort(key=lambda row: (-row[0], row[1].uuid))
    return scored
