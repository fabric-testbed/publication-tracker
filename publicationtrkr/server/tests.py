"""
Tests for the 1.12.1 security batch.

Two things here fail silently in production if they are wrong, which is why they are
the ones with tests. The /api/ write guard rejects requests, so a mistake in its
content-type matching either lets the forgeable shape through or breaks every JSON
client. The JWT claim checks are worse: a wrong expectation makes jwt.decode() raise,
the caller swallows the exception, and every visitor is quietly anonymous.
"""

import base64
import gzip
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import jwt
from django.test import RequestFactory, SimpleTestCase

from publicationtrkr.apps.apiuser.models import ApiUser
from publicationtrkr.server.middleware import ApiSimpleRequestGuardMiddleware
from publicationtrkr.utils.fabric_auth import (
    JWKS_KID_MISS_REFETCH_SECONDS,
    _cached_jwks_keys,
    _expected_token_issuer,
    _kid_absent,
    _kid_miss_refetch_allowed,
    _signing_key_for,
    get_oidc_sub_from_cookie,
)

VOUCH_SECRET = 'a' * 44


def _ok(request):
    from django.http import HttpResponse
    return HttpResponse('reached the view')


class ApiSimpleRequestGuardTests(SimpleTestCase):
    """
    The guard exists because DRF wraps every view in csrf_exempt while identity comes
    from a cookie the browser attaches by itself.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = ApiSimpleRequestGuardMiddleware(_ok)

    def test_form_encoded_post_to_api_is_rejected(self):
        # The exact shape the finding describes: a CORS simple request, so no preflight
        # gets a veto, and DRF's FormParser would have accepted it.
        request = self.factory.post(
            '/api/publications', data={'title': 'x'},
            content_type='application/x-www-form-urlencoded')
        self.assertEqual(self.middleware(request).status_code, 403)

    def test_multipart_and_text_plain_are_rejected_too(self):
        for content_type in ('multipart/form-data; boundary=----x', 'text/plain'):
            with self.subTest(content_type=content_type):
                request = self.factory.post('/api/publications', data='x',
                                            content_type=content_type)
                self.assertEqual(self.middleware(request).status_code, 403)

    def test_the_non_simple_header_lets_it_through(self):
        # A non-safelisted header forces a preflight, which a non-allowlisted origin
        # fails -- so the request can no longer be forged from one.
        request = self.factory.post(
            '/api/publications', data={'title': 'x'},
            content_type='application/x-www-form-urlencoded',
            headers={'x-requested-with': 'XMLHttpRequest'})
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_json_writes_are_untouched(self):
        # application/json is itself non-simple. Every curl example in the README uses
        # it, and none of them may start needing a header.
        request = self.factory.post('/api/publications', data='{}',
                                    content_type='application/json')
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_reads_are_untouched(self):
        request = self.factory.get('/api/publications')
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_the_html_form_path_is_untouched(self):
        # The form views post form-encoded to their own Django URLs, where
        # CsrfViewMiddleware and {% csrf_token %} already apply. Path-scoping is the
        # whole reason this is a middleware and not a DRF permission class.
        request = self.factory.post('/publications/create', data={'title': 'x'},
                                    content_type='application/x-www-form-urlencoded')
        self.assertEqual(self.middleware(request).status_code, 200)

    def test_a_path_merely_starting_with_api_is_not_scoped_in(self):
        request = self.factory.post('/apiusers/', data={'x': '1'},
                                    content_type='application/x-www-form-urlencoded')
        self.assertEqual(self.middleware(request).status_code, 200)


class VouchCookieClaimTests(SimpleTestCase):
    """
    The cookie is decoded with a shared HMAC secret, which says who signed it but not
    who it was signed for. The issuer check is what closes that.
    """

    @staticmethod
    def _cookie(claims):
        token = jwt.encode(claims, VOUCH_SECRET, algorithm='HS256')
        return base64.urlsafe_b64encode(gzip.compress(token.encode())).decode()

    def test_a_matching_issuer_authenticates(self):
        with mock.patch.dict(os.environ, {'VOUCH_JWT_SECRET': VOUCH_SECRET,
                                          'VOUCH_JWT_ISSUER': 'Vouch'}):
            cookie = self._cookie({'iss': 'Vouch', 'CustomClaims': {'sub': 'sub-1'}})
            self.assertEqual(get_oidc_sub_from_cookie(cookie=cookie), 'sub-1')

    def test_another_relying_party_on_the_same_secret_is_rejected(self):
        # Same secret, different issuer: the cookie verifies cryptographically and used
        # to authenticate. This is the finding.
        with mock.patch.dict(os.environ, {'VOUCH_JWT_SECRET': VOUCH_SECRET,
                                          'VOUCH_JWT_ISSUER': 'Vouch'}):
            cookie = self._cookie({'iss': 'SomeOtherVouch',
                                   'CustomClaims': {'sub': 'sub-1'}})
            self.assertIsNone(get_oidc_sub_from_cookie(cookie=cookie))

    def test_an_empty_expectation_disables_the_check(self):
        # The documented escape hatch for a deployment whose Vouch stamps something
        # else: a variable, not a release.
        with mock.patch.dict(os.environ, {'VOUCH_JWT_SECRET': VOUCH_SECRET,
                                          'VOUCH_JWT_ISSUER': ''}):
            cookie = self._cookie({'iss': 'anything', 'CustomClaims': {'sub': 'sub-1'}})
            self.assertEqual(get_oidc_sub_from_cookie(cookie=cookie), 'sub-1')

    def test_audience_is_only_checked_when_one_is_configured(self):
        # Vouch stamps no top-level 'aud' by default, so verifying unconditionally would
        # only be a way to lock every visitor out.
        env = {'VOUCH_JWT_SECRET': VOUCH_SECRET, 'VOUCH_JWT_ISSUER': 'Vouch'}
        with mock.patch.dict(os.environ, dict(env, VOUCH_JWT_AUDIENCE='')):
            cookie = self._cookie({'iss': 'Vouch', 'aud': 'somebody',
                                   'CustomClaims': {'sub': 'sub-1'}})
            self.assertEqual(get_oidc_sub_from_cookie(cookie=cookie), 'sub-1')
        with mock.patch.dict(os.environ, dict(env, VOUCH_JWT_AUDIENCE='us')):
            cookie = self._cookie({'iss': 'Vouch', 'aud': 'somebody',
                                   'CustomClaims': {'sub': 'sub-1'}})
            self.assertIsNone(get_oidc_sub_from_cookie(cookie=cookie))
            cookie = self._cookie({'iss': 'Vouch', 'aud': 'us',
                                   'CustomClaims': {'sub': 'sub-1'}})
            self.assertEqual(get_oidc_sub_from_cookie(cookie=cookie), 'sub-1')

    def test_a_garbled_cookie_still_returns_none(self):
        # The failure log reads the JWT back to name the offending claim; it must not
        # raise when there is no JWT to read.
        with mock.patch.dict(os.environ, {'VOUCH_JWT_SECRET': VOUCH_SECRET}):
            self.assertIsNone(get_oidc_sub_from_cookie(cookie='not-a-cookie'))


class SigningKeySelectionTests(SimpleTestCase):
    """keys[0] was taken unconditionally, so a CM rotation broke all token auth."""

    KEY_A = {'kid': 'a', 'kty': 'RSA', 'n': 'aaa', 'e': 'AQAB'}
    KEY_B = {'kid': 'b', 'kty': 'RSA', 'n': 'bbb', 'e': 'AQAB'}

    def test_the_kid_is_matched_not_the_position(self):
        self.assertEqual(_signing_key_for([self.KEY_A, self.KEY_B], 'b'), self.KEY_B)

    def test_no_kid_falls_back_to_the_first_key(self):
        self.assertEqual(_signing_key_for([self.KEY_A, self.KEY_B], None), self.KEY_A)

    def test_an_unknown_kid_raises_rather_than_signing_with_the_wrong_key(self):
        with self.assertRaises(jwt.PyJWKError):
            _signing_key_for([self.KEY_A], 'c')

    def test_an_old_single_key_cache_row_still_reads(self):
        # Releases before 1.12.1 cached one JWK object, not a key set. An upgrade must
        # not require invalidating that row by hand.
        import json

        class Row:
            pass

        legacy, modern = Row(), Row()
        legacy.value = json.dumps(self.KEY_A)
        modern.value = json.dumps({'keys': [self.KEY_A, self.KEY_B]})
        self.assertEqual(_cached_jwks_keys(legacy), [self.KEY_A])
        self.assertEqual(_cached_jwks_keys(modern), [self.KEY_A, self.KEY_B])

    def test_an_unreadable_cache_row_is_empty_not_an_exception(self):
        class Row:
            value = 'not json'

        self.assertEqual(_cached_jwks_keys(Row()), [])


class ExpectedTokenIssuerTests(SimpleTestCase):
    """
    Off by default, on purpose. FABRIC bearer tokens are to be issued as
    'fabric-core-api' on uis and that is not in place yet, so there is no value to
    default to -- and a guessed one would reject every bearer-token caller silently.
    """

    def test_it_is_unset_by_default(self):
        with mock.patch.dict(os.environ, {'FABRIC_TOKEN_ISSUER': ''}):
            self.assertIsNone(_expected_token_issuer())

    def test_it_does_not_fall_back_to_the_credential_manager_url(self):
        # An earlier draft defaulted to FABRIC_CREDENTIAL_MANAGER on the reasoning that
        # whoever signs the token issues it. That was a guess, and a wrong guess here
        # costs every API caller their authentication.
        with mock.patch.dict(os.environ, {'FABRIC_CREDENTIAL_MANAGER': 'https://cm.example/',
                                          'FABRIC_TOKEN_ISSUER': ''}):
            self.assertIsNone(_expected_token_issuer())

    def test_an_explicit_value_turns_the_check_on(self):
        # The path forward: an .env change and a restart once uis emits the issuer.
        with mock.patch.dict(os.environ, {'FABRIC_TOKEN_ISSUER': 'fabric-core-api'}):
            self.assertEqual(_expected_token_issuer(), 'fabric-core-api')

    def test_whitespace_is_not_an_expectation(self):
        # None, not '' -- PyJWT treats an empty string as a real expectation and would
        # reject every token that does not carry a literal empty issuer.
        with mock.patch.dict(os.environ, {'FABRIC_TOKEN_ISSUER': '   '}):
            self.assertIsNone(_expected_token_issuer())


class IsProjectMemberTests(SimpleTestCase):
    """Finding 15: as a @property taking an argument it could never be evaluated."""

    def test_it_is_callable_with_a_project_uuid(self):
        api_user = ApiUser(uuid='u-1', projects=['p-1', 'p-2'])
        self.assertTrue(api_user.is_project_member('p-1'))
        self.assertFalse(api_user.is_project_member('p-3'))


class JwksRefetchRateLimitTests(SimpleTestCase):
    """
    Selecting the key by kid made an unrecognised kid a reason to re-read
    /credmgr/certs -- and what kid a token names is chosen by whoever sends it. Without
    a bound, a caller gets one outbound HTTPS request per request, each holding a uwsgi
    worker for up to FABRIC_HTTP_TIMEOUT, on the HTML views too where nothing throttles.
    Before this release the fetch fired only on the 24h cache expiry and no token could
    provoke it, so this guards a regression this release introduced.
    """

    class Row:
        def __init__(self, age_seconds=None):
            self.last_updated = (
                None if age_seconds is None
                else datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
            )

    def test_a_known_kid_is_never_a_refetch_reason(self):
        keys = [{'kid': 'a'}, {'kid': 'b'}]
        self.assertFalse(_kid_absent(keys, 'b'))
        self.assertFalse(_kid_absent(keys, None))

    def test_an_unknown_kid_is_a_refetch_reason(self):
        self.assertTrue(_kid_absent([{'kid': 'a'}], 'rotated-in'))

    def test_a_fresh_cache_refuses_a_caller_driven_refetch(self):
        # The attacker's request: cache written a moment ago, token names a kid that is
        # not in it. No outbound call.
        self.assertFalse(_kid_miss_refetch_allowed(self.Row(age_seconds=1)))

    def test_recovery_is_still_quick_after_a_real_rotation(self):
        self.assertTrue(
            _kid_miss_refetch_allowed(self.Row(age_seconds=JWKS_KID_MISS_REFETCH_SECONDS + 1))
        )

    def test_a_row_that_was_never_written_always_allows_the_read(self):
        self.assertTrue(_kid_miss_refetch_allowed(self.Row(age_seconds=None)))
