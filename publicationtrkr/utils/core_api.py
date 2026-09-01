import os

import requests

# Outbound HTTP timeout, in seconds. These calls run inline during request handling --
# publication create/update validates project_uuid against core-api on every write --
# so an unbounded request holds a uwsgi worker until the peer gives up.
CORE_API_TIMEOUT = float(os.getenv('FABRIC_HTTP_TIMEOUT_SECONDS', '10'))


class BearerAuth(requests.auth.AuthBase):
    def __init__(self, token):
        self.token = token

    def __call__(self, r):
        r.headers["authorization"] = "Bearer " + self.token
        return r


def query_core_api_by_cookie(query: str, cookie: str):
    """
    Issue a simple GET query against core-api using cookie auth
    """
    s = requests.Session()
    response = None
    try:
        s.cookies.set(os.getenv('VOUCH_COOKIE_NAME'), cookie)
        api_call = s.get(url=os.getenv('FABRIC_CORE_API') + query, timeout=CORE_API_TIMEOUT)
        response = api_call.json()
    except Exception as exc:
        print(exc)
    s.close()
    return response


def query_core_api_by_token(query: str, token: str):
    """
    Issue a simple GET query against core-api using token auth
    """
    s = requests.Session()
    response = None
    try:
        s.auth = BearerAuth(token=token)
        api_call = s.get(url=os.getenv('FABRIC_CORE_API') + query, timeout=CORE_API_TIMEOUT)
        response = api_call.json()
    except Exception as exc:
        print(exc)
    s.close()
    return response
