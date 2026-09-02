from django.http import JsonResponse

# Methods that cannot change state, and so need no guard.
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS', 'TRACE'})

# The three content types a browser will send cross-origin without a preflight. A
# request limited to these plus safelisted headers is a CORS "simple request": the
# browser sends it, cookies and all, and withholds only the *response* from the calling
# script. CORS therefore never gets a veto over the write itself.
SIMPLE_CONTENT_TYPES = frozenset({
    'application/x-www-form-urlencoded',
    'multipart/form-data',
    'text/plain',
})

# Any header outside the CORS-safelisted set forces a preflight. X-Requested-With is the
# conventional choice and is already listed in CORS_ALLOW_HEADERS.
NON_SIMPLE_HEADER = 'X-Requested-With'

FORGEABLE_WRITE_DETAIL = (
    "Unsafe methods under /api/ require an '{0}' header when the request body is "
    "form-encoded, multipart or plain text. Send application/json instead, or add "
    "the header.".format(NON_SIMPLE_HEADER)
)


class ApiSimpleRequestGuardMiddleware:
    """
    Reject cross-site-forgeable writes under /api/.

    DRF's APIView.as_view() wraps every view in csrf_exempt, and this project sets
    DEFAULT_AUTHENTICATION_CLASSES to [], so DRF's own SessionAuthentication CSRF
    enforcement never runs either. Identity comes from the Vouch cookie, which a browser
    attaches to cross-origin requests automatically. That left
    POST /api/publications with Content-Type: application/x-www-form-urlencoded
    forgeable from any origin: it is a CORS simple request, so there is no preflight to
    block, and DRF's FormParser accepts it. PUT/PATCH/DELETE already required a
    preflight and were blocked for non-allowlisted origins; POST was not.

    Requiring a non-simple header on unsafe methods forces a preflight, which a
    non-allowlisted origin fails. JSON bodies are unaffected -- application/json is
    itself non-simple -- so ordinary API clients and every curl example in the README
    keep working unchanged.

    Path-scoped to /api/ deliberately, rather than written as a DRF permission class.
    The HTML form views call PublicationViewSet(request=request).create(...) in-process
    rather than over HTTP (publications/views.py), so they never reach DRF dispatch and
    a permission class would not see them. They post to their own Django views, where
    CsrfViewMiddleware and {% csrf_token %} already apply, and are untouched by this.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self.is_forgeable_write(request):
            return JsonResponse({'detail': FORGEABLE_WRITE_DETAIL}, status=403)
        return self.get_response(request)

    @staticmethod
    def is_forgeable_write(request) -> bool:
        if not request.path.startswith('/api/'):
            return False
        if request.method in SAFE_METHODS:
            return False
        # request.content_type is already stripped of its parameters (charset, boundary)
        # by Django; split() again so a hand-built test request is handled too.
        content_type = (request.content_type or '').split(';')[0].strip().lower()
        if content_type not in SIMPLE_CONTENT_TYPES:
            return False
        return not request.headers.get(NON_SIMPLE_HEADER)
