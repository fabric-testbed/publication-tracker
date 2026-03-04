import os

import requests


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
        api_call = s.get(url=os.getenv('FABRIC_CORE_API') + query)
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
        api_call = s.get(url=os.getenv('FABRIC_CORE_API') + query)
        response = api_call.json()
    except Exception as exc:
        print(exc)
    s.close()
    return response
