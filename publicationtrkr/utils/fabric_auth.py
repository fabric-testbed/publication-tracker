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
    try:
        # get base64 encoded gzipped vouch JWT
        base64_encoded_gzip_vouch_jwt = cookie
        # decode base64
        encoded_gzip_vouch_jwt_bytes = base64.urlsafe_b64decode(base64_encoded_gzip_vouch_jwt)
        # gzip decompress
        vouch_jwt_bytes = gzip.decompress(encoded_gzip_vouch_jwt_bytes)
        # decode bytes
        vouch_jwt = vouch_jwt_bytes.decode('utf-8')
        # decode JWT using Vouch Proxy secret key (do not verify aud)
        vouch_json = jwt.decode(
            jwt=vouch_jwt,
            key=os.getenv('VOUCH_JWT_SECRET'),
            algorithms=["HS256"],
            options={"verify_aud": False}
        )
        # vouch_jwt holder for decoded JWT
        oidc_sub = vouch_json.get('CustomClaims').get('sub')
        return oidc_sub
    except Exception as exc:
        print(exc)
        return None


def get_oidc_sub_from_token(token: str) -> str | None:
    s = requests.Session()
    try:
        psk = TaskTimeoutTracker.objects.get(name=os.getenv('PSK_NAME'))
        if not psk.timed_out():
            public_signing_key = jwt.PyJWK(json.loads(psk.value)).key
        else:
            api_call = s.get(url=os.getenv('FABRIC_CREDENTIAL_MANAGER') + '/credmgr/certs',
                              timeout=FABRIC_HTTP_TIMEOUT)
            jwks = api_call.json().get('keys')[0]
            public_signing_key = jwt.PyJWK(jwks).key
            psk.value = json.dumps(jwks)
            psk.last_updated = datetime.now(timezone.utc)
            psk.save()
        token_json = jwt.decode(
            jwt=token,
            key=public_signing_key,
            algorithms=["RS256"],
            options={"verify_aud": False}
        )
        oidc_sub = token_json.get('sub')
    except Exception as exc:
        print(exc)
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
            projects = []
            fabric_roles = []
            for r in fab_person.json().get('results', [])[0].get('roles'):
                if is_valid_uuid(r.get('name')[:-3]):
                    projects.append(r.get('name')[:-3])
                    continue
                else:
                    fabric_roles.append(r.get('name'))

            api_user.projects = list(set(projects))
            api_user.fabric_roles = list(set(fabric_roles))
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
            projects = []
            fabric_roles = []
            for r in fab_person.json().get('results', [])[0].get('roles'):
                if is_valid_uuid(r.get('name')[:-3]):
                    projects.append(r.get('name')[:-3])
                    continue
                else:
                    fabric_roles.append(r.get('name'))

            api_user.projects = list(set(projects))
            api_user.fabric_roles = list(set(fabric_roles))
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
