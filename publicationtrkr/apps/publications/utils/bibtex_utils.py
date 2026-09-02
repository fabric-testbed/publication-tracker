import bibtexparser

# Fields every entry must carry for a publication to be built from it. The single
# entry parser stays lenient (its callers validate separately); parse_bibtex_entries
# enforces these so the bulk path can name what is wrong with entry 47 of 200.
REQUIRED_BIBTEX_FIELDS = ('authors', 'title', 'year')

# Entry types whose venue is a booktitle rather than a journal.
PROCEEDINGS_ENTRY_TYPES = frozenset({'inproceedings', 'conference', 'incollection', 'proceedings'})

DEFAULT_ENTRY_TYPE = 'article'


def _resolve_author_names(publication) -> list:
    """
    Resolve Publication.authors (list of Author UUIDs) to author_name strings.
    Falls back gracefully if Author records are missing.
    """
    from publicationtrkr.apps.publications.models import Author
    if not publication.authors:
        return []
    author_map = {a.uuid: a.author_name for a in Author.objects.filter(uuid__in=publication.authors)}
    return [author_map[u] for u in publication.authors if u in author_map]


def _entry_to_fields(entry: dict) -> dict:
    """
    Map one bibtexparser entry onto publication field names.

    Missing fields come back None rather than absent, so callers can rely on a
    fixed shape. entry_type is carried through because generate_bibtex needs it to
    stop hardcoding @article, and because it decides whether venue is a journal or
    a booktitle.
    """
    fields = {
        'authors': None,
        'entry_type': (entry.get('ENTRYTYPE') or DEFAULT_ENTRY_TYPE).strip().lower(),
        'link': None,
        'title': None,
        'venue': None,
        'year': None,
    }

    # author -> split on " and " -> authors list
    if entry.get('author'):
        names = [a.strip() for a in entry['author'].split(' and ') if a.strip()]
        fields['authors'] = names or None

    # title
    if entry.get('title'):
        fields['title'] = entry['title']

    # year
    if entry.get('year'):
        fields['year'] = entry['year']

    # journal or booktitle -> venue
    if entry.get('journal'):
        fields['venue'] = entry['journal']
    elif entry.get('booktitle'):
        fields['venue'] = entry['booktitle']

    # url or doi -> link
    if entry.get('url'):
        fields['link'] = entry['url']
    elif entry.get('doi'):
        fields['link'] = 'https://doi.org/{0}'.format(entry['doi'])

    return fields


def parse_bibtex(bibtex_string: str) -> dict:
    """
    Parse a BibTeX string and return the fields of its FIRST entry.
    Returns {authors, entry_type, link, title, venue, year} with None for missing
    fields. Never raises: the create, update and form paths all call this on
    user-supplied text and treat a failed parse as "no defaults available".
    """
    empty = _entry_to_fields({})
    empty['entry_type'] = None
    try:
        bib_database = bibtexparser.loads(bibtex_string)
        if not bib_database.entries:
            return empty
        return _entry_to_fields(bib_database.entries[0])
    except Exception:
        return empty


def parse_bibtex_entries(bibtex_string: str) -> tuple:
    """
    Parse EVERY entry in a BibTeX string, reporting what could not be used.

    Returns (entries, errors). entries holds one field dict per usable entry, each
    carrying its 'index' in the source document; errors holds
    {'index': int|None, 'error': str} for entries that are unusable, with a None
    index when the document as a whole could not be read.

    parse_bibtex() keeps its lenient single-entry contract for the existing
    callers. This is the one the bulk endpoint needs, where a 200-entry upload has
    to say which entries were skipped and why instead of silently taking the first.
    """
    try:
        bib_database = bibtexparser.loads(bibtex_string)
    except Exception as exc:
        return [], [{'index': None, 'error': 'unable to parse BibTeX: {0}'.format(exc)}]

    entries = []
    errors = []
    for index, entry in enumerate(bib_database.entries):
        try:
            fields = _entry_to_fields(entry)
        except Exception as exc:
            errors.append({'index': index, 'error': 'unable to read entry: {0}'.format(exc)})
            continue
        missing = [name for name in REQUIRED_BIBTEX_FIELDS if not fields.get(name)]
        if missing:
            errors.append({'index': index, 'error': 'missing required field(s): {0}'.format(', '.join(missing))})
            continue
        fields['index'] = index
        # The entry re-serialised on its own, so a bulk upload can store what was
        # actually uploaded rather than the five fields this module maps. It also
        # carries the entry type forward: Publication has no column for it, but
        # generate_bibtex reads it back out of a stored string.
        fields['bibtex'] = dumps_entry(entry)
        entries.append(fields)
    return entries, errors


def dumps_entry(entry: dict) -> str:
    """Serialise one bibtexparser entry back to a single-entry BibTeX string."""
    database = bibtexparser.bibdatabase.BibDatabase()
    database.entries = [entry]
    return bibtexparser.dumps(database).strip()


def generate_bibtex(publication, entry_type: str = None) -> str:
    """
    Generate a BibTeX string from a Publication model instance.
    Citation key = first author last name + year.

    entry_type defaults to the type recorded in publication.bibtex when there is
    one, and to @article otherwise -- Publication has no column for it, so a record
    built from form fields alone has nothing better to offer.
    """
    author_names = _resolve_author_names(publication)

    if not entry_type and getattr(publication, 'bibtex', None):
        entry_type = parse_bibtex(publication.bibtex).get('entry_type')
    entry_type = (entry_type or DEFAULT_ENTRY_TYPE).strip().lower()
    venue_field = 'booktitle' if entry_type in PROCEEDINGS_ENTRY_TYPES else 'journal'

    # Build citation key from first author last name + year
    key = 'unknown'
    if author_names:
        first_author = author_names[0]
        # Take last word as last name
        parts = first_author.strip().split()
        if parts:
            key = parts[-1].lower()
    if publication.year:
        key = key + publication.year

    lines = ['@{0}{{{1},'.format(entry_type, key)]

    if publication.title:
        lines.append('  title = {{{0}}},'.format(publication.title))

    if author_names:
        lines.append('  author = {{{0}}},'.format(' and '.join(author_names)))

    if publication.year:
        lines.append('  year = {{{0}}},'.format(publication.year))

    if publication.venue:
        lines.append('  {0} = {{{1}}},'.format(venue_field, publication.venue))

    if publication.link:
        lines.append('  url = {{{0}}},'.format(publication.link))

    # Remove trailing comma from last field line
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(',')

    lines.append('}')

    return '\n'.join(lines)
