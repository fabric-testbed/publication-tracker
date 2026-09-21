"""Offline recovery tests for human-approved TeamBrain capture batches."""

import copy
import json
import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock

from scripts.capture_approved_memories import CaptureError, LIST_LIMIT, capture_batch, main, proposal_tag


SLUG = 'fabric-testbed/publication-tracker-dev'
PR_URL = f'https://github.com/{SLUG}/pull/123'
PAYLOAD = {
    'project_slug': SLUG, 'pr_number': 123, 'pr_url': PR_URL, 'merge_sha': 'abc123',
    'proposals': [
        {'content': 'First approved decision.', 'type': 'decision', 'tags': ['reviewed']},
        {'content': 'Second approved convention.', 'type': 'convention', 'tags': []},
        {'content': 'Third approved context.', 'type': 'context'},
    ],
}


def stored(proposal, identifier='existing', **overrides):
    return {
        'id': identifier, 'content': proposal['content'], 'type': proposal.get('type', 'context'),
        'scope': 'project', 'tags': [], 'linked_pr_url': PR_URL, **overrides,
    }


class CaptureBatchTests(TestCase):
    def test_partial_failure_retries_only_missing_proposal(self):
        rows = []
        posted = []
        failures = {'Second approved convention.'}

        def request(method, query=None, body=None):
            if method == 'GET':
                self.assertEqual(query['project_slug'], SLUG)
                self.assertEqual(query['linked_pr_url'], PR_URL)
                return {'project_slug': SLUG, 'count': len(rows), 'results': copy.deepcopy(rows)}
            posted.append(body['content'])
            if body['content'] in failures:
                raise RuntimeError('simulated unavailable service')
            result = {**body, 'id': f'id-{len(rows)}'}
            rows.append(result)
            return result

        first = capture_batch(PAYLOAD, SLUG, request)
        self.assertEqual((first['captured'], first['already_present'], first['failed']), (2, 0, 1))
        self.assertFalse(first['complete'])
        failures.clear()
        second = capture_batch(PAYLOAD, SLUG, request)
        self.assertEqual((second['captured'], second['already_present'], second['failed']), (1, 2, 0))
        self.assertTrue(second['complete'])
        self.assertEqual(posted, [p['content'] for p in PAYLOAD['proposals']] + ['Second approved convention.'])
        third = capture_batch(PAYLOAD, SLUG, request)
        self.assertEqual((third['captured'], third['already_present']), (0, 3))
        self.assertEqual(len(rows), 3)

    def test_legacy_content_type_match_skips_only_that_proposal(self):
        existing = [stored(PAYLOAD['proposals'][0])]
        request = mock.Mock(side_effect=[
            {'project_slug': SLUG, 'count': 1, 'results': existing}, {'id': 'new-1'}, {'id': 'new-2'},
        ])
        report = capture_batch(PAYLOAD, SLUG, request)
        self.assertEqual((report['captured'], report['already_present']), (2, 1))
        self.assertEqual(request.call_count, 3)

    def test_unrelated_memory_for_the_same_pr_does_not_block_batch(self):
        request = mock.Mock(side_effect=[
            {'project_slug': SLUG, 'count': 1, 'results': [stored({'content': 'Unrelated'}, type='context')]},
            {'id': 'new-1'}, {'id': 'new-2'}, {'id': 'new-3'},
        ])
        report = capture_batch(PAYLOAD, SLUG, request)
        self.assertEqual((report['captured'], report['already_present']), (3, 0))

    def test_durable_tag_recognizes_a_previously_captured_then_edited_memory(self):
        rows = [stored(p, f'existing-{i}', content='Edited memory', tags=[proposal_tag(SLUG, PR_URL, p)])
                for i, p in enumerate(PAYLOAD['proposals'])]
        request = mock.Mock(return_value={'project_slug': SLUG, 'count': len(rows), 'results': rows})
        report = capture_batch(PAYLOAD, SLUG, request)
        self.assertEqual((report['captured'], report['already_present']), (0, 3))
        request.assert_called_once()

    def test_failed_malformed_or_truncated_reads_never_attempt_capture(self):
        responses = [
            RuntimeError('token=must-not-log'), {'error': 'denied'},
            {'project_slug': SLUG, 'count': 2, 'results': []},
            {'project_slug': SLUG, 'count': LIST_LIMIT,
             'results': [stored(PAYLOAD['proposals'][0])] * LIST_LIMIT},
        ]
        for response in responses:
            with self.subTest(response_type=type(response).__name__):
                request = mock.Mock(side_effect=response) if isinstance(response, Exception) else mock.Mock(return_value=response)
                with self.assertRaises(CaptureError):
                    capture_batch(PAYLOAD, SLUG, request)
                request.assert_called_once()

    def test_wrong_project_payload_cannot_write_to_another_project(self):
        request = mock.Mock()
        with self.assertRaises(CaptureError):
            capture_batch({**PAYLOAD, 'project_slug': 'another/repository'}, SLUG, request)
        request.assert_not_called()

    def test_unconfirmed_post_is_failed_and_recovers_if_write_actually_landed(self):
        payload = {**PAYLOAD, 'proposals': [PAYLOAD['proposals'][0]]}
        request = mock.Mock(side_effect=[{'project_slug': SLUG, 'count': 0, 'results': []}, {}])
        first = capture_batch(payload, SLUG, request)
        self.assertEqual((first['captured'], first['failed']), (0, 1))
        self.assertFalse(first['complete'])
        request = mock.Mock(return_value={
            'project_slug': SLUG, 'count': 1, 'results': [stored(PAYLOAD['proposals'][0])],
        })
        retry = capture_batch(payload, SLUG, request)
        self.assertTrue(retry['complete'])
        self.assertEqual(retry['already_present'], 1)
        request.assert_called_once()

    def test_duplicate_proposals_do_not_retry_an_uncertain_post_within_the_batch(self):
        payload = {**PAYLOAD, 'proposals': [PAYLOAD['proposals'][0]] * 2}
        request = mock.Mock(side_effect=[
            {'project_slug': SLUG, 'count': 0, 'results': []}, RuntimeError('uncertain outcome'),
        ])
        report = capture_batch(payload, SLUG, request)
        self.assertEqual((report['captured'], report['failed']), (0, 2))
        self.assertEqual(request.call_count, 2)

    def test_failed_cli_report_keeps_issue_open_and_does_not_expose_errors(self):
        with TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ('payload.json', 'report.json', 'comment.md')]
            paths[0].write_text(json.dumps(PAYLOAD))
            output = StringIO()
            with mock.patch('sys.argv', ['capture-helper', *map(str, paths)]), \
                    mock.patch.dict(os.environ, {
                        'PROJECT_SLUG': SLUG, 'TEAMBRAIN_BASE': 'https://example.invalid',
                        'TEAMBRAIN_ACCESS_TOKEN': 'secret-must-not-appear',
                    }), \
                    mock.patch('scripts.capture_approved_memories.TeamBrainClient',
                               return_value=mock.Mock(side_effect=RuntimeError('secret-must-not-appear'))), \
                    redirect_stdout(output):
                self.assertEqual(main(), 1)
            self.assertFalse(json.loads(paths[1].read_text())['complete'])
            self.assertIn('Issue remains open', paths[2].read_text())
            self.assertNotIn('secret-must-not-appear', output.getvalue() + paths[2].read_text())
