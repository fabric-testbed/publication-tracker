"""Capture an already human-approved batch with resumable per-proposal deduplication.

Authorization stays in capture-on-merge.yml. This helper never approves proposals,
posts GitHub comments, or closes issues. HTTP POSTs are not retried automatically:
an uncertain response is reconciled against stored thoughts on the next approved run.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LIST_LIMIT = 100  # TeamBrain REST supports no pagination; a full page is ambiguous.
TYPES = {'decision', 'convention', 'gotcha', 'context', 'preference', 'runbook'}


class CaptureError(Exception):
    """Safe operational error: never include credentials or remote response bodies."""


def proposal_tag(project_slug, pr_url, proposal):
    identity = [project_slug, pr_url, proposal.get('type') or 'context', proposal['content']]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return 'tb-capture:' + digest


def capture_batch(payload, project_slug, request):
    """Return counts/results; request(method, query=..., body=...) is injectable."""
    if not isinstance(payload, dict) or payload.get('project_slug') != project_slug:
        raise CaptureError('Invalid payload project; no captures attempted.')
    pr_url = payload.get('pr_url')
    pr_number = payload.get('pr_number')
    if (not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number < 1
            or pr_url != f'https://github.com/{project_slug}/pull/{pr_number}'):
        raise CaptureError('Invalid payload PR; no captures attempted.')
    proposals = payload.get('proposals')
    if not isinstance(proposals, list) or not proposals:
        raise CaptureError('Missing approved proposals; no captures attempted.')
    for proposal in proposals:
        if (not isinstance(proposal, dict) or not isinstance(proposal.get('content'), str)
                or not proposal['content'].strip() or len(proposal['content']) > 10000
                or (proposal.get('type') or 'context') not in TYPES
                or not isinstance(proposal.get('tags', []), list)
                or any(not isinstance(tag, str) for tag in proposal.get('tags', []))):
            raise CaptureError('Invalid approved proposal; no captures attempted.')

    try:
        existing = request('GET', query={
            'project_slug': project_slug, 'linked_pr_url': pr_url,
            'scopes': 'project', 'limit': LIST_LIMIT,
        })
    except Exception:
        raise CaptureError('Existing-thought lookup failed; no captures attempted.') from None
    if (not isinstance(existing, dict) or existing.get('project_slug') != project_slug
            or not isinstance(existing.get('results'), list)
            or existing.get('count') != len(existing['results'])
            or len(existing['results']) >= LIST_LIMIT):
        raise CaptureError('Existing-thought lookup was invalid or incomplete; no captures attempted.')
    rows = existing['results']
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
                or not isinstance(row.get('content'), str)
                or row.get('scope') != 'project' or row.get('linked_pr_url') != pr_url
                or not isinstance(row.get('tags', []), list)):
            raise CaptureError('Existing-thought record was invalid; no captures attempted.')

    report = {'total': len(proposals), 'captured': 0, 'already_present': 0, 'failed': 0, 'results': []}
    uncertain = set()
    for index, proposal in enumerate(proposals):
        memory_type = proposal.get('type') or 'context'
        tag = proposal_tag(project_slug, pr_url, proposal)
        match = next((row for row in rows if tag in row.get('tags', []) or (
            row.get('content') == proposal['content'] and row.get('type') == memory_type
        )), None)
        if match:
            report['already_present'] += 1
            report['results'].append({'index': index, 'status': 'already_present', 'id': match['id']})
            continue
        if tag in uncertain:
            # Repeated identical proposals must not repeat a POST whose outcome
            # is unknown. The next approved run will reconcile the stored result.
            report['failed'] += 1
            report['results'].append({'index': index, 'status': 'failed'})
            continue
        body = {
            'content': proposal['content'], 'type': memory_type, 'scope': 'project',
            'project_slug': project_slug, 'linked_pr_url': pr_url,
            'linked_commit_sha': payload.get('merge_sha') or '',
            'tags': sorted(set(proposal.get('tags', []) + [
                'pr-merge', 'auto-capture', f'{project_slug}#{pr_number}', tag,
            ])),
        }
        try:
            result = request('POST', body=body)
            if not isinstance(result, dict) or not isinstance(result.get('id'), str) or not result['id']:
                raise CaptureError('Capture returned no confirmed ID.')
        except Exception:
            uncertain.add(tag)
            report['failed'] += 1
            report['results'].append({'index': index, 'status': 'failed'})
            # No response body/exception is logged: either could echo credentials.
            continue
        report['captured'] += 1
        report['results'].append({'index': index, 'status': 'captured', 'id': result['id']})
        rows.append({**body, 'id': result['id']})
    report['complete'] = report['failed'] == 0
    return report


class TeamBrainClient:
    def __init__(self, base_url, access_token):
        self.url = base_url.rstrip('/') + '/teambrain-rest/thoughts'
        self.access_token = access_token

    def __call__(self, method, *, query=None, body=None):
        url = self.url + ('?' + urlencode(query) if query else '')
        request = Request(url, method=method, headers={
            'Authorization': 'Bearer ' + self.access_token,
            'Content-Type': 'application/json',
        }, data=json.dumps(body).encode() if body is not None else None)
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError):
            raise CaptureError('TeamBrain request failed or returned invalid JSON.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('payload')
    parser.add_argument('report')
    parser.add_argument('comment')
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.payload).read_text())
        report = capture_batch(payload, os.environ['PROJECT_SLUG'], TeamBrainClient(
            os.environ['TEAMBRAIN_BASE'], os.environ['TEAMBRAIN_ACCESS_TOKEN']))
    except Exception:
        report = {'complete': False, 'captured': 0, 'already_present': 0, 'failed': None}
        comment = ('Capture could not establish the approved batch or verify existing thoughts. '
                   'No captures attempted. Issue remains open; retry /approve after resolving the run failure.')
    else:
        comment = (f"Approved batch: {report['captured']} newly captured, "
                   f"{report['already_present']} already present, {report['failed']} failed "
                   f"out of {report['total']} proposal(s). ")
        comment += ('All approved proposals are present. Closing.' if report['complete'] else
                    'Issue remains open. Retry /approve to capture only missing proposals.')
    Path(args.report).write_text(json.dumps(report) + '\n')
    Path(args.comment).write_text(comment + '\n')
    print(comment)
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
