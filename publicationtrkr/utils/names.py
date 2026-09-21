"""Display-name normalization shared by directory sync and authentication."""


def normalize_person_name(value: str | None) -> str:
    """Collapse whitespace without changing spelling or using names as identity keys."""
    return ' '.join((value or '').split())
