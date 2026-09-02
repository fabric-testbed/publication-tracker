import base64
import gzip
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import requests

from publicationtrkr.apps.apiuser.models import ApiUser, TaskTimeoutTracker

# Outbound HTTP timeout, in seconds, for every call in this module. All of them sit on
# the authentication path, so an unbounded request holds a uwsgi worker (4 processes x
# 2 threads = 8 concurrent slots) until the peer gives up -- a trivially reachable
# denial of service, and the trigger for the fail-open revocation list fixed below.
FABRIC_HTTP_TIMEOUT = float(os.getenv('FABRIC_HTTP_TIMEOUT_SECONDS', '10'))


# Expected 'iss' and 'aud' for the two JWTs this module accepts.
#
# Neither was checked before: the cookie path verified only that the HMAC matched and
# the token path only that the credential manager's key signed it. A shared secret or a
# shared signing key says who signed a JWT, not who it was signed *for*, so any FABRIC
# service on the same secret or the same CM minted credentials that authenticated here.
#
# Every value below is env-overridable and empty means "not checked". That is the escape
# hatch, not a default to leave alone: an expectation that does not match reality fails
# silently -- the decode raises, the caller's except swallows it, and the visitor is
# quietly anonymous -- so the alternative to an override is an outage nobody can read.
# _log_claim_mismatch() prints the value a real JWT actually carries when one is
# rejected, which is where an override's value comes from.


def _env_or_none(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default or '').strip() or None


def _expected_vouch_issuer() -> str | None:
    """
    'iss' the Vouch Proxy cookie is expected to carry.

    Vouch stamps its own issuer, which defaults to 'Vouch' and differs only where a
    deployment set vouch.jwt.issuer in vouch/config -- so such a deployment needs a
    variable, not a release.
    """
    return _env_or_none('VOUCH_JWT_ISSUER', 'Vouch')


def _expected_token_issuer() -> str | None:
    """
    'iss' a FABRIC bearer token is expected to carry. Unset by default.

    There is no correct value to default to. A real token observed 2026-09-01 carries
    iss='https://cilogon.org' -- shared by every CILogon-issued token, so pinning it
    excludes almost nothing -- and FABRIC bearer tokens are in any case to be reissued as
    'fabric-core-api' on uis, which would make a pinned value silently wrong on the day
    that lands. Prefer _expected_token_audience() for this path.

    An earlier draft defaulted this to FABRIC_CREDENTIAL_MANAGER, reasoning that whoever
    holds the signing key issues the token. The observed iss shows that was wrong; had it
    shipped, every bearer-token caller would have been rejected, and the rejection is
    swallowed (see _log_claim_mismatch) so it would have surfaced as "the API stopped
    authenticating anyone" rather than as a configuration error.

    So this half of the check is deliberately off until the value is known. Set
    FABRIC_TOKEN_ISSUER=fabric-core-api once uis emits it -- an .env change and a
    restart, no release. The rehearsal command in the 1.12.1 upgrade notes prints the
    claims a real token actually carries.

    Note this is unrelated to FABRIC_CORE_API_TOKEN / FABRIC_CORE_API_SERVICES_TOKEN.
    Those are shared secrets, not JWTs -- they are sent as bearer credentials to
    core-api by sync_fabric_users and never decoded here.
    """
    return _env_or_none('FABRIC_TOKEN_ISSUER')


def _expected_vouch_audience() -> str | None:
    """
    Unset by default: Vouch stamps no top-level 'aud' in its shipped configuration, so
    verifying one unconditionally would only add a way to lock every visitor out.
    """
    return _env_or_none('VOUCH_JWT_AUDIENCE')


def _expected_token_audience() -> str | None:
    """
    Unset by default, but this is the check worth turning on for bearer tokens.

    A real FABRIC token carries iss='https://cilogon.org' and
    aud='cilogon:/client_id/<id>'. Every CILogon-issued token shares that issuer, so the
    issuer excludes almost nothing; the audience names the specific CILogon client, which
    is what separates a token minted for FABRIC from one minted for another relying party
    whose tokens the same signing key would also verify.

    Left unset all the same, because the client id differs per deployment and a wrong
    value rejects every API caller silently. Read the real one off the decode command in
    the 1.12.1 upgrade notes.
    """
    return _env_or_none('FABRIC_TOKEN_AUDIENCE')


def _log_claim_mismatch(exc, encoded_jwt: str, source: str) -> None:
    """
    Explain an issuer/audience rejection in terms of the variable that fixes it.

    A wrong expectation fails silently everywhere else in this module: the decode
    raises, the caller's except swallows it, and the visitor is quietly downgraded to
    anonymous -- which reads as "the deployment is broken", not "one string is wrong".
    The claims are re-read WITHOUT signature verification purely to name the offending
    value in a log line. Nothing here is returned to the caller or trusted.
    """
    if not isinstance(exc, (jwt.InvalidIssuerError, jwt.InvalidAudienceError,
                            jwt.MissingRequiredClaimError)):
        return
    try:
        claims = jwt.decode(jwt=encoded_jwt, options={'verify_signature': False})
    except Exception:
        return
    print(
        "{0} rejected: {1}. Token carries iss={2!r}, aud={3!r}. If those are correct, "
        "set the matching *_ISSUER / *_AUDIENCE variable in .env.".format(
            source, type(exc).__name__, claims.get('iss'), claims.get('aud')
        )
    )


def _cached_jwks_keys(psk) -> list:
    """
    Read the cached credential-manager key set out of a TaskTimeoutTracker row.

    Releases before 1.12.1 cached a single JWK object, because keys[0] was all that was
    ever used. Both shapes are accepted so an existing cache row does not have to be
    invalidated by hand on upgrade.
    """
    try:
        cached = json.loads(psk.value)
    except Exception:
        return []
    if isinstance(cached, dict) and isinstance(cached.get('keys'), list):
        return cached['keys']
    if isinstance(cached, dict) and cached:
        return [cached]
    if isinstance(cached, list):
        return cached
    return []


# Minimum seconds between /credmgr/certs re-reads that were triggered by a token naming a
# 'kid' the cache does not hold.
#
# Selecting the key by kid means an unrecognised kid is a reason to suspect the cache has
# missed a rotation -- but "the caller sent an unknown kid" is caller-controlled, and
# re-reading on every such token hands anyone an outbound HTTPS request per request, each
# holding a uwsgi worker for up to FABRIC_HTTP_TIMEOUT. get_api_user() runs on the HTML
# views as well, where no throttle applies. Bounding the rate keeps rotation recovery
# quick while making the fetch rate independent of what callers send.
JWKS_KID_MISS_REFETCH_SECONDS = 60


def _kid_absent(jwks_keys: list, kid: str | None) -> bool:
    return bool(kid) and not any(k.get('kid') == kid for k in jwks_keys)


def _kid_miss_refetch_allowed(psk) -> bool:
    if not psk.last_updated:
        return True
    age = (datetime.now(timezone.utc) - psk.last_updated).total_seconds()
    return age >= JWKS_KID_MISS_REFETCH_SECONDS


def _signing_key_for(jwks_keys: list, kid: str | None):
    """
    Pick the JWKS entry matching the token's 'kid'.

    keys[0] was taken unconditionally, so a credential-manager key rotation that lists
    the new key second broke every bearer token until the cached JWKS expired. Falls
    back to the first key only when the token names no kid, which is the pre-rotation
    single-key case.
    """
    if kid:
        for key in jwks_keys:
            if key.get('kid') == kid:
                return key
        raise jwt.PyJWKError("no signing key in /credmgr/certs matches kid '{0}'".format(kid))
    return jwks_keys[0]


def get_api_user(request) -> ApiUser:
    """
    Get API user
    - check for recent access to artifact-manager
    - if recent access not found, check for token and/or cookie settings
    - if found - use against core-api to get user details
    - store user details for short-term access (API_USER_REFRESH_CHECK_MINUTES)
    - if not found - return anonymous api_user object
    """
    try:
        api_user = ApiUser.objects.filter(uuid=os.getenv('API_USER_ANON_UUID')).first()
        cookie = request.COOKIES.get(os.getenv('VOUCH_COOKIE_NAME'), None)
        token = request.headers.get('authorization', 'Bearer ').replace('Bearer ', '')
        now = datetime.now(timezone.utc)
        if token and not is_token_revoked(token=token):
            oidc_sub = get_oidc_sub_from_token(token=token)
            if oidc_sub:
                api_user = ApiUser.objects.filter(cilogon_id=oidc_sub).first()
                if api_user:
                    # access_expires is nullable. Comparing None to a datetime raises
                    # TypeError, which the broad except below swallows -- silently
                    # downgrading a valid caller to anonymous. A row synced from
                    # core-api rather than created by a login has no access_expires,
                    # so this stops being hypothetical once user sync lands.
                    if api_user.access_expires and api_user.access_expires > now:
                        return api_user
                api_user = auth_user_by_token(token=token)
                api_user.access_expires = now + timedelta(minutes=int(os.getenv('API_USER_REFRESH_CHECK_MINUTES')))
                api_user.save()
        if cookie:
            oidc_sub = get_oidc_sub_from_cookie(cookie=cookie)
            if oidc_sub:
                api_user = ApiUser.objects.filter(cilogon_id=oidc_sub).first()
                if api_user:
                    # access_expires is nullable. Comparing None to a datetime raises
                    # TypeError, which the broad except below swallows -- silently
                    # downgrading a valid caller to anonymous. A row synced from
                    # core-api rather than created by a login has no access_expires,
                    # so this stops being hypothetical once user sync lands.
                    if api_user.access_expires and api_user.access_expires > now:
                        return api_user
                api_user = auth_user_by_cookie(cookie=cookie)
                api_user.access_expires = now + timedelta(minutes=int(os.getenv('API_USER_REFRESH_CHECK_MINUTES')))
                api_user.save()
    except Exception as exc:
        print(exc)
        api_user = ApiUser.objects.filter(uuid=os.getenv('API_USER_ANON_UUID')).first()
    # return api user
    return api_user


def get_oidc_sub_from_cookie(cookie: str) -> str | None:
    # Bound before the try so the failure log below can reference it even when the
    # base64/gzip unwrapping is what failed.
    vouch_jwt = ''
    try:
        # get base64 encoded gzipped vouch JWT
        base64_encoded_gzip_vouch_jwt = cookie
        # decode base64
        encoded_gzip_vouch_jwt_bytes = base64.urlsafe_b64decode(base64_encoded_gzip_vouch_jwt)
        # gzip decompress
        vouch_jwt_bytes = gzip.decompress(encoded_gzip_vouch_jwt_bytes)
        # decode bytes
        vouch_jwt = vouch_jwt_bytes.decode('utf-8')
        # decode JWT using the Vouch Proxy secret key.
        #
        # issuer= and audience= were both absent: the only thing checked was that the
        # HMAC verified. Any FABRIC service sharing this VOUCH_JWT_SECRET therefore
        # minted cookies that authenticated here, because a shared secret says who
        # signed a token but not who it was signed for.
        #
        # verify_aud follows whether an expected audience is configured. Vouch does not
        # stamp a top-level 'aud' in its default configuration, so turning the check on
        # unconditionally would only add a way to lock everyone out.
        expected_issuer = _expected_vouch_issuer()
        expected_audience = _expected_vouch_audience()
        vouch_json = jwt.decode(
            jwt=vouch_jwt,
            key=os.getenv('VOUCH_JWT_SECRET'),
            algorithms=["HS256"],
            issuer=expected_issuer,
            audience=expected_audience,
            options={"verify_aud": expected_audience is not None,
                     "verify_iss": expected_issuer is not None}
        )
        # vouch_jwt holder for decoded JWT
        oidc_sub = vouch_json.get('CustomClaims').get('sub')
        return oidc_sub
    except Exception as exc:
        print(exc)
        _log_claim_mismatch(exc, vouch_jwt, 'vouch cookie')
        return None


def get_oidc_sub_from_token(token: str) -> str | None:
    s = requests.Session()
    try:
        # Select the signing key by the token's own 'kid' rather than taking keys[0].
        # The cache holds the whole key set now; a row written by an older release holds
        # a single key object, which still reads correctly through the same path.
        kid = jwt.get_unverified_header(token).get('kid')
        psk = TaskTimeoutTracker.objects.get(name=os.getenv('PSK_NAME'))
        jwks_keys = [] if psk.timed_out() else _cached_jwks_keys(psk)
        # Re-read /credmgr/certs when there is nothing cached, or when the token names a
        # kid the cache does not hold and we have not already re-read recently. The
        # second case is caller-controlled, hence the rate limit -- see
        # JWKS_KID_MISS_REFETCH_SECONDS. When it is refused, the key selection below
        # raises and the caller falls through to anonymous, which is the same outcome an
        # unknown kid had before, minus the outbound request.
        if not jwks_keys or (_kid_absent(jwks_keys, kid) and _kid_miss_refetch_allowed(psk)):
            api_call = s.get(url=os.getenv('FABRIC_CREDENTIAL_MANAGER') + '/credmgr/certs',
                              timeout=FABRIC_HTTP_TIMEOUT)
            jwks_keys = api_call.json().get('keys')
            psk.value = json.dumps({'keys': jwks_keys})
            psk.last_updated = datetime.now(timezone.utc)
            psk.save()
        public_signing_key = jwt.PyJWK(_signing_key_for(jwks_keys, kid)).key
        # issuer= and audience= were both absent here too: a token minted by anything
        # holding the credential manager's signing key authenticated. See the cookie
        # path above for why verify_aud follows an explicit configured audience.
        expected_issuer = _expected_token_issuer()
        expected_audience = _expected_token_audience()
        token_json = jwt.decode(
            jwt=token,
            key=public_signing_key,
            algorithms=["RS256"],
            issuer=expected_issuer,
            audience=expected_audience,
            options={"verify_aud": expected_audience is not None,
                     "verify_iss": expected_issuer is not None}
        )
        oidc_sub = token_json.get('sub')
    except Exception as exc:
        print(exc)
        _log_claim_mismatch(exc, token, 'fabric bearer token')
        oidc_sub = None

    s.close()
    return oidc_sub


def auth_user_by_cookie(cookie: str) -> ApiUser:
    """
    Use cookie to authorize user
    - get user uuid from core-api using cookie
    - with user uuid populate user information from core-api /people/{uuid}?as_self=true
    """
    s = requests.Session()
    try:
        s.cookies.set(os.getenv('VOUCH_COOKIE_NAME'), cookie)
        whoami = s.get(url=os.getenv('FABRIC_CORE_API') + '/whoami', timeout=FABRIC_HTTP_TIMEOUT)
        api_user_uuid = whoami.json().get('results', [])[0].get('uuid', os.getenv('API_USER_ANON_UUID'))
        if api_user_uuid and api_user_uuid != os.getenv('API_USER_ANON_UUID'):
            api_user = ApiUser.objects.filter(uuid=api_user_uuid).first()
            if not api_user:
                api_user = ApiUser()
            api_user.uuid = api_user_uuid
            fab_person = s.get(url=os.getenv('FABRIC_CORE_API') + '/people/{0}?as_self=true'.format(api_user.uuid),
                              timeout=FABRIC_HTTP_TIMEOUT)
            api_user.affiliation = fab_person.json().get('results', [])[0].get('affiliation')
            api_user.email = fab_person.json().get('results', [])[0].get('email')
            api_user.name = fab_person.json().get('results', [])[0].get('name')
            api_user.cilogon_id = fab_person.json().get('results', [])[0].get('cilogon_id')
            api_user.access_type = ApiUser.COOKIE
            # This row now has a real session behind it, whether it was created here or
            # by sync_fabric_users. cilogon_id was set just above, which is the join key
            # a synced row is missing until its owner first logs in.
            api_user.has_logged_in = True
            roles = [r.get('name') for r in fab_person.json().get('results', [])[0].get('roles')]
            api_user.projects, api_user.fabric_roles = split_fabric_roles(roles)
        else:
            api_user = ApiUser.objects.filter(uuid=os.getenv('API_USER_ANON_UUID')).first()
    except Exception as exc:
        print(exc)
        api_user = ApiUser.objects.filter(uuid=os.getenv('API_USER_ANON_UUID')).first()
    s.close()
    return api_user


def auth_user_by_token(token):
    """
    Use token to authorize user
    - get user uuid from core-api using cookie
    - with user uuid populate user information from core-api /people/{uuid}?as_self=true
    """
    s = requests.Session()
    try:
        s.headers['Authorization'] = 'Bearer {0}'.format(token)
        whoami = s.get(url=os.getenv('FABRIC_CORE_API') + '/whoami', timeout=FABRIC_HTTP_TIMEOUT)
        api_user_uuid = whoami.json().get('results', [])[0].get('uuid', os.getenv('API_USER_ANON_UUID'))
        if api_user_uuid and api_user_uuid != os.getenv('API_USER_ANON_UUID'):
            api_user = ApiUser.objects.filter(uuid=api_user_uuid).first()
            if not api_user:
                api_user = ApiUser()
            api_user.uuid = api_user_uuid
            fab_person = s.get(url=os.getenv('FABRIC_CORE_API') + '/people/{0}?as_self=true'.format(api_user.uuid),
                              timeout=FABRIC_HTTP_TIMEOUT)
            api_user.affiliation = fab_person.json().get('results', [])[0].get('affiliation')
            api_user.email = fab_person.json().get('results', [])[0].get('email')
            api_user.name = fab_person.json().get('results', [])[0].get('name')
            api_user.cilogon_id = fab_person.json().get('results', [])[0].get('cilogon_id')
            api_user.access_type = ApiUser.TOKEN
            # This row now has a real session behind it, whether it was created here or
            # by sync_fabric_users. cilogon_id was set just above, which is the join key
            # a synced row is missing until its owner first logs in.
            api_user.has_logged_in = True
            roles = [r.get('name') for r in fab_person.json().get('results', [])[0].get('roles')]
            api_user.projects, api_user.fabric_roles = split_fabric_roles(roles)
        else:
            api_user = ApiUser.objects.filter(uuid=os.getenv('API_USER_ANON_UUID')).first()
    except Exception as exc:
        print(exc)
        api_user = ApiUser.objects.filter(uuid=os.getenv('API_USER_ANON_UUID')).first()
    s.close()
    return api_user


def is_token_revoked(token: str) -> bool:
    """
    Check an incoming token against the Token Revocation List (TRL)

    Fails CLOSED. If no revocation list can be obtained at all -- not from the cache,
    not from the Credential Manager -- the token is treated as revoked. An unreachable
    TRL is precisely the situation in which a revoked token would otherwise keep
    working, so "unknown" has to mean "deny".

    Denying here only skips the bearer-token branch of get_api_user(); a request that
    also carries a valid Vouch cookie still authenticates by cookie.
    """
    revocation_list = get_token_revocation_list()
    if revocation_list is None:
        print('TRL unavailable - refusing token authentication')
        return True
    try:
        token_hash = hashlib.new('sha256')
        token_hash.update(token.encode())
        if token_hash.hexdigest() in revocation_list:
            return True
    except Exception as exc:
        print(exc)
        return True
    return False


def get_token_revocation_list() -> list | None:
    """
    Retrieve Token Revocation List (TRL) from CM at some interval

    Returns None when no list could be obtained, which the caller treats as "deny".
    Returning an empty list for that case is what made revocation fail open: an empty
    list is indistinguishable from "nothing is revoked", so every token read as valid
    for the duration of any CM outage.

    A stale cached list still describes every token revoked up to the moment it was
    written, so a failed refresh falls back to it rather than discarding it.
    """
    trl = None
    try:
        trl = TaskTimeoutTracker.objects.get(name=os.getenv('TRL_NAME'))
        if not trl.timed_out() and trl.value:
            return list(json.loads(trl.value))
    except Exception as exc:
        print(exc)

    s = requests.Session()
    try:
        api_call = s.get(url=os.getenv('FABRIC_CREDENTIAL_MANAGER') + '/credmgr/tokens/revoke_list',
                         timeout=FABRIC_HTTP_TIMEOUT)
        api_call.raise_for_status()
        token_revocation_list = api_call.json().get('data')
        if token_revocation_list is None:
            raise ValueError('revoke_list response contained no "data" key')
        if trl:
            trl.value = json.dumps(token_revocation_list)
            trl.last_updated = datetime.now(timezone.utc)
            trl.save()
        return list(token_revocation_list)
    except Exception as exc:
        print(exc)
    finally:
        s.close()

    # Refresh failed -- fall back to the last cached list if there is one.
    try:
        if trl and trl.value:
            print('TRL refresh failed - falling back to cached revocation list')
            return list(json.loads(trl.value))
    except Exception as exc:
        print(exc)
    return None


def is_valid_uuid(val) -> bool:
    try:
        uuid.UUID(str(val))
        return True
    except ValueError:
        return False


# Project roles come back from core-api as '<project_uuid><suffix>', where the suffix is
# one of these. Matched explicitly rather than by the older "strip any three characters
# and see whether a UUID is left" idiom: same result today, but it states the contract
# instead of implying it, and it will not silently absorb some future three-character
# suffix that ought to mean something else. Mirrors core-api's own people_utils.py.
# '-pc' was removed in core-api v1.10.0 and is deliberately absent.
PROJECT_ROLE_SUFFIXES = ('-pm', '-po', '-tk')


def split_fabric_roles(roles) -> tuple[list[str], list[str]]:
    """
    Split a flat list of core-api role names into (projects, fabric_roles).

    Anything that is not '<project_uuid><suffix>' is a global FABRIC role --
    'Jupyterhub', 'fabric-active-users', 'publication-tracker-admins', 'project-leads',
    and so on. That set is deliberately open-ended: a global role we have never seen
    falls through to fabric_roles, which is the right default, so new ones need no
    change here.

    Both lists are de-duplicated and sorted, so re-reading the same roles produces the
    same value and the sync does not record a spurious change.

    This lived inline and verbatim in both auth_user_by_cookie and auth_user_by_token;
    sync_fabric_users would have made it a third copy.
    """
    projects = []
    fabric_roles = []
    for role in roles or []:
        if not role:
            continue
        for suffix in PROJECT_ROLE_SUFFIXES:
            if role.endswith(suffix) and is_valid_uuid(role[:-len(suffix)]):
                projects.append(role[:-len(suffix)])
                break
        else:
            fabric_roles.append(role)
    return sorted(set(projects)), sorted(set(fabric_roles))
