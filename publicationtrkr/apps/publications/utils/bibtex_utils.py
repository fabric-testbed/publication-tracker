import bibtexparser


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


def parse_bibtex(bibtex_string: str) -> dict:
    """
    Parse a BibTeX string and return extracted fields.
    Returns {authors, title, year, venue, link} with None for missing fields.
    """
    result = {
        'authors': None,
        'title': None,
        'year': None,
        'venue': None,
        'link': None,
    }
    try:
        bib_database = bibtexparser.loads(bibtex_string)
        if not bib_database.entries:
            return result
        entry = bib_database.entries[0]

        # author -> split on " and " -> authors list
        if 'author' in entry and entry['author']:
            result['authors'] = [a.strip() for a in entry['author'].split(' and ')]

        # title
        if 'title' in entry and entry['title']:
            result['title'] = entry['title']

        # year
        if 'year' in entry and entry['year']:
            result['year'] = entry['year']

        # journal or booktitle -> venue
        if 'journal' in entry and entry['journal']:
            result['venue'] = entry['journal']
        elif 'booktitle' in entry and entry['booktitle']:
            result['venue'] = entry['booktitle']

        # url or doi -> link
        if 'url' in entry and entry['url']:
            result['link'] = entry['url']
        elif 'doi' in entry and entry['doi']:
            result['link'] = 'https://doi.org/{0}'.format(entry['doi'])

    except Exception:
        pass

    return result


def generate_bibtex(publication) -> str:
    """
    Generate a BibTeX @article string from a Publication model instance.
    Citation key = first author last name + year.
    """
    author_names = _resolve_author_names(publication)

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

    lines = ['@article{{{0},'.format(key)]

    if publication.title:
        lines.append('  title = {{{0}}},'.format(publication.title))

    if author_names:
        lines.append('  author = {{{0}}},'.format(' and '.join(author_names)))

    if publication.year:
        lines.append('  year = {{{0}}},'.format(publication.year))

    if publication.venue:
        lines.append('  journal = {{{0}}},'.format(publication.venue))

    if publication.link:
        lines.append('  url = {{{0}}},'.format(publication.link))

    # Remove trailing comma from last field line
    if len(lines) > 1:
        lines[-1] = lines[-1].rstrip(',')

    lines.append('}')

    return '\n'.join(lines)
