import re

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.customization import splitname

# Fields every entry must carry for a publication to be built from it. The single
# entry parser stays lenient (its callers validate separately); parse_bibtex_entries
# enforces these so the bulk path can name what is wrong with entry 47 of 200.
REQUIRED_BIBTEX_FIELDS = ('authors', 'title', 'year')

# Entry types whose venue is a booktitle rather than a journal.
PROCEEDINGS_ENTRY_TYPES = frozenset({'inproceedings', 'conference', 'incollection', 'proceedings'})

DEFAULT_ENTRY_TYPE = 'article'

# One character that cannot appear in a BibTeX field, used to blank out braced spans so
# that a separator search only ever sees the top level.
_MASK = '\x00'

# The BibTeX name separator: " and " surrounded by whitespace, at brace depth 0.
_AND = re.compile(r'\s+and\s+')


# Month names spelled out. BibTeX defines the three-letter macros jan..dec, which
# `common_strings` supplies, but not these -- and 30 of the 188 stored entries in
# production carry a bare `month=July` or `month=June`, which publisher exports
# (Crossref's in particular) emit freely. Without them bibtexparser raises
# UndefinedString and parse_bibtex returns *no fields at all*, so a whole entry silently
# stops being readable over one word in a field nothing here even stores. Found while
# checking #66's round-trip criterion against production, where it made 16% of the
# corpus un-re-importable.
_LONG_MONTHS = {
    'january': 'January', 'february': 'February', 'march': 'March', 'april': 'April',
    'june': 'June', 'july': 'July', 'august': 'August', 'september': 'September',
    'sept': 'September', 'october': 'October', 'november': 'November',
    'december': 'December',
}


def _bibtex_parser() -> BibTexParser:
    """
    A parser that can read what publishers actually emit.

    A fresh one per call: bibtexparser accumulates the strings and entries it has seen on
    the parser object, so a shared instance would leak one document's macros into the
    next.
    """
    parser = BibTexParser(common_strings=True)
    parser.bib_database.strings.update(_LONG_MONTHS)
    return parser


def _mask_braced(value: str) -> str:
    """
    A same-length copy of `value` with everything inside braces blanked out.

    Searching the mask and slicing the original is what makes the separator search
    brace-aware without hand-rolling a scanner: `{Ministry of Health and Welfare}` is one
    author, and `{Smith, John}` is one name that must not be un-inverted, because the
    braces are BibTeX's way of saying "treat this as a unit".
    """
    out = []
    depth = 0
    for char in value:
        if char == '{':
            depth += 1
            out.append(_MASK)
        elif char == '}':
            depth = max(depth - 1, 0)
            out.append(_MASK)
        else:
            out.append(_MASK if depth else char)
    return ''.join(out)


def _strip_outer_braces(name: str) -> str:
    """`{The FABRIC Team}` is a literal name; the braces are markup, not part of it."""
    while len(name) > 1 and name.startswith('{') and name.endswith('}') \
            and _mask_braced(name).strip(_MASK) == '':
        name = name[1:-1].strip()
    return name


def split_author_field(author_field: str) -> list:
    """
    Split a BibTeX `author` field into individual names.

    The separator is " and " **at brace depth 0**. The old code split the raw string, so
    a braced corporate author or any surname containing the word "and" was cut in half.
    """
    if not author_field:
        return []
    value = ' '.join(author_field.split())
    mask = _mask_braced(value)
    names, start = [], 0
    for match in _AND.finditer(mask):
        names.append(value[start:match.start()])
        start = match.end()
    names.append(value[start:])
    return [name.strip() for name in names if name.strip()]


def normalize_author_name(name: str) -> str:
    """
    Un-invert one BibTeX name: `"Grigoryan, Garegin"` -> `"Garegin Grigoryan"`.

    `"Last, First"` is the dominant BibTeX convention, and nothing here ever un-inverted
    it (#66). The stored strings that came out of that are what #62 had to repair by hand,
    so this is the half of the fix that stops them coming back.

    **A name with no top-level comma is returned untouched** beyond whitespace tidying.
    That is deliberate: `splitname` would happily re-derive `"Cees de Laat"` from itself,
    but it would also have an opinion about names it was never given a comma to interpret,
    and quietly rewriting an author who is already stored correctly is exactly the harm
    #62 spent its length undoing. A comma is the author saying which part is the surname;
    without one there is nothing to act on.

    Never raises. Every caller reaches this through `parse_bibtex`, whose contract is that
    unparseable input yields no defaults rather than an error.
    """
    if not name:
        return ''
    value = ' '.join(name.split())
    if ',' not in _mask_braced(value):
        return _strip_outer_braces(value)
    try:
        parts = splitname(value, strict_mode=False)
    except Exception:
        return _strip_outer_braces(value)
    ordered = parts.get('first', []) + parts.get('von', []) + parts.get('last', []) \
        + parts.get('jr', [])
    rebuilt = ' '.join(piece for piece in ordered if piece).strip()
    return _strip_outer_braces(rebuilt) or _strip_outer_braces(value)


def author_names(author_field: str) -> list:
    """
    Every name in a BibTeX `author` field, split and un-inverted, in printed order.

    A bare `others` is dropped. In BibTeX `and others` is *et al.*, not a person, and
    storing it made an `Author` row that a real human could claim -- which is what
    `aef4e78c` had before #62 repaired it. The truncation is not lost: the entry is stored
    verbatim in `Publication.bibtex`, which still says `and others`. A braced `{others}`
    survives, because braces are the author asserting that it is a literal name.
    """
    names = []
    for part in split_author_field(author_field):
        if part.strip().lower() == 'others':
            continue
        name = normalize_author_name(part)
        if name:
            names.append(name)
    return names


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

    # author -> split on " and " at depth 0 -> un-invert "Last, First" -> authors list
    if entry.get('author'):
        fields['authors'] = author_names(entry['author']) or None

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
        bib_database = bibtexparser.loads(bibtex_string, parser=_bibtex_parser())
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
        bib_database = bibtexparser.loads(bibtex_string, parser=_bibtex_parser())
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


def _protect_name(name: str) -> str:
    """
    Brace a stored name that would not survive being read back.

    `author` fields are joined on " and ", and a comma inside one means "Last, First", so
    a corporate author like `Ministry of Health and Welfare` would come back as two
    authors and `Smith, John` would come back inverted. Bracing says "this is one literal
    name", which is exactly what split_author_field and normalize_author_name honour --
    so generate -> parse round-trips.
    """
    if name.startswith('{') and name.endswith('}'):
        return name
    if _AND.search(name) or ',' in name:
        return '{{{0}}}'.format(name)
    return name


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
        lines.append('  author = {{{0}}},'.format(
            ' and '.join(_protect_name(name) for name in author_names)))

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
