"""Template context shared by every rendered page.

Views in this project build their context dicts by hand and pass `api_user`
explicitly, which works because every one of them is a page a user asked for. The
release number is different: it is rendered by base.html's footer, so it has to be
present on every response including the ones no view of ours produced -- Django's
error pages among them. A context processor is the only place that holds for all of
them.
"""

from django.conf import settings


def app_version(request):
    """Expose the release number read from pyproject.toml as `app_version`."""
    return {"app_version": settings.APP_VERSION}
