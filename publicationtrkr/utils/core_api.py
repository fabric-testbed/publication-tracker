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


# The sync runs off the request path (management command / cron sidecar), so it does not
# hold a uwsgi worker and can afford to wait longer than CORE_API_TIMEOUT for a response
# carrying the whole FABRIC population.
CORE_API_SYNC_TIMEOUT = float(os.getenv('FABRIC_SYNC_HTTP_TIMEOUT_SECONDS', '60'))

# /journey-tracker/people rejects a window wider than 90 days. 89 leaves a day of slack
# so an inclusive-endpoint interpretation on the server cannot push a window over.
JOURNEY_TRACKER_MAX_WINDOW_DAYS = 89

# The endpoint parses start_date / end_date with '%Y-%m-%d %H:%M:%S%z' after appending
# '+0000' itself, so an ISO-8601 string with a 'T' or a 'Z' is rejected with HTTP 400.
JOURNEY_TRACKER_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


def get_journey_tracker_people(start_date, end_date, token: str) -> list[dict]:
    """
    Fetch every FABRIC person whose record was updated within [start_date, end_date).

    Peer-service ingest endpoint on core-api; no Core API change was needed for this.
    Returns rows of fabric_uuid / name / email_address / affiliation / active /
    fabric_roles / fabric_registered_on / fabric_last_seen. Notably it does NOT return
    cilogon_id -- see sync_fabric_users for why that resolves itself.

    Not paginated: the whole window comes back in one response.

    Raises rather than returning an empty list on failure. A caller that advanced a
    sync watermark past a window it never actually read would skip those people until
    the next full backfill, so "no data" and "no answer" must not look alike.
    """
    s = requests.Session()
    try:
        s.auth = BearerAuth(token=token)
        api_call = s.get(
            url=os.getenv('FABRIC_CORE_API') + '/journey-tracker/people',
            params={
                'start_date': start_date.strftime(JOURNEY_TRACKER_DATE_FORMAT),
                'end_date': end_date.strftime(JOURNEY_TRACKER_DATE_FORMAT),
            },
            timeout=CORE_API_SYNC_TIMEOUT,
        )
        api_call.raise_for_status()
        return api_call.json().get('results') or []
    finally:
        s.close()
