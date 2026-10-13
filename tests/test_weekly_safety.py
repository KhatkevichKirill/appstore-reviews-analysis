"""Regression tests run exported Code nodes; no network or production writes."""
import json
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from test_workflows import NODE, ROOT, code_nodes, load
import test_workflows as tw
import sys
sys.path.insert(0, os.path.join(ROOT, 'scripts'))
import ratings_report as rr

WF = load('app-store-ratings-weekly-report.json')
CODES = code_nodes(WF)


def run_code(name, data=None, context=None, execution_id='test-execution'):
    src = '''const [code, data, context, id] = process.argv.slice(1).map(JSON.parse);
const $ = name => ({first: () => ({json: context[name]}), all: () => context[name]});
const input = data.map(json => ({json}));
console.log(JSON.stringify(new Function('$', '$input', '$execution', code)(
  $, {first: () => input[0], all: () => input}, {id})));'''
    out = subprocess.run([NODE, '-e', src, json.dumps(CODES[name]), json.dumps(data or []),
                          json.dumps(context or {}), json.dumps(execution_id)], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr)
    return [r['json'] for r in json.loads(out.stdout)]


@unittest.skipUnless(NODE, 'node is not installed')
class ComparisonSafetyTest(unittest.TestCase):
    run_report = tw.WeeklyReportTest.run_report
    HARNESS = tw.WeeklyReportTest.HARNESS

    def rows(self, partial_current, partial_previous):
        start = datetime(2026, 9, 28, tzinfo=timezone.utc)
        rows = []
        for h in range(0, 24 * 14 + 7, 6):
            at = start + timedelta(hours=h)
            if partial_previous and at < datetime(2026, 10, 1, tzinfo=timezone.utc):
                continue
            if partial_current and at > datetime(2026, 10, 9, tzinfo=timezone.utc):
                continue
            prev_n = min(h // 6, 28) * 10
            cur_n = max(h // 6 - 28, 0) * 20
            n = 1000 + prev_n + cur_n
            avg = (4500 + prev_n * 4 + cur_n * 4.5) / n
            for cc in ('US', 'GB', 'CA', 'AU', 'BR', 'IN'):
                rows.append(dict(snapshot_at=at.isoformat(), country=cc,
                                 user_rating_count=str(n), average_user_rating=f'{avg:.5f}'))
        return rows

    def test_all_four_completeness_cases_and_python_parity(self):
        for current_partial in (False, True):
            for previous_partial in (False, True):
                with self.subTest(current=current_partial, previous=previous_partial):
                    rows = self.rows(current_partial, previous_partial)
                    out = self.run_report(rows, [], '2026-10-12T12:17:00Z')
                    py = {(s.start.strftime('%Y-%m-%d'), s.country): s
                          for s in rr.compute(rr.load_snapshots(rows), [], period='week')}
                    for cur, prev in zip(out['report']['current'], out['report']['previous']):
                        cc = cur['country'].upper()
                        self.assertEqual(bool(cur['partial']), current_partial)
                        self.assertEqual(bool(prev['partial']), previous_partial)
                        expected = rr.compare_periods(py[('2026-10-05', cc)], py[('2026-09-28', cc)])
                        for key, value in expected.items():
                            if current_partial or previous_partial:
                                self.assertIsNone(cur['comparison'][key])
                            else:
                                self.assertAlmostEqual(cur['comparison'][key], float(value), places=9)
                        if not current_partial and not previous_partial:
                            self.assertAlmostEqual(cur['comparison']['countPercent'], 100)
                            self.assertAlmostEqual(cur['comparison']['meanDelta'], .5, places=3)
                    table = out['slackBody']['blocks'][1]['text']['text']
                    if current_partial or previous_partial:
                        self.assertNotIn('%', table)
                        self.assertIn('comparisons are omitted', out['slackBody']['blocks'][2]['elements'][0]['text'])

    def test_oct12_cannot_compare_with_oct1_partial_baseline(self):
        out = self.run_report(self.rows(False, True), [], '2026-10-12T12:17:00Z')
        for row in out['slackBody']['blocks'][1]['text']['text'].strip('`\n').splitlines()[1:]:
            self.assertEqual(row.count('—'), 2, row)

    def test_zero_baseline_reset_missing_and_inconsistent(self):
        current = rr.PeriodStats(datetime.now(timezone.utc), 'US', new_ratings=20, new_average=rr.Decimal('4.5'))
        previous = rr.PeriodStats(current.start, 'US', new_ratings=0, new_average=None)
        self.assertEqual(rr.compare_periods(current, previous), {'countPercent': None, 'meanDelta': None})
        previous.new_ratings = 10
        previous.new_average = rr.Decimal('4')
        for flag in ('partial', 'no_data', 'count_decreased'):
            for row in (current, previous):
                row.flags = [flag]
                self.assertEqual(rr.compare_periods(current, previous), {'countPercent': None, 'meanDelta': None})
                row.flags = []
        current.flags = ['inconsistent']
        self.assertEqual(rr.compare_periods(current, previous), {'countPercent': 100, 'meanDelta': None})


@unittest.skipUnless(NODE, 'node is not installed')
class DeliveryCodeSafetyTest(unittest.TestCase):
    def delivery(self, enabled=True, channel='CTEST'):
        report = {'report': {'weekStart': 1791158400000}, 'slackBody': {'text': 'report', 'blocks': []}}
        return run_code('Prepare delivery', [report], {'Delivery settings': {
            'deliveryEnabled': enabled, 'channelId': channel, 'appId': '1209815023'}})[0]

    def test_preview_default_and_no_claim(self):
        settings = run_code('Delivery settings')[0]
        self.assertIs(settings['deliveryEnabled'], False)
        for enabled in (False, 'true', 1, None):
            delivery = self.delivery(enabled)
            self.assertTrue(delivery['preview'])
            self.assertNotIn('key', delivery)
            self.assertEqual(run_code('Build guarded request', [{'state': 'sending'}],
                {'Prepare claim': delivery}), [])

    def test_report_key_is_stable_across_execution_ids(self):
        report = {'report': {'weekStart': 1791158400000}, 'slackBody': {'text': 'report'}}
        context = {'Delivery settings': {'deliveryEnabled': True, 'channelId': 'CTEST', 'appId': '1209815023'}}
        first = run_code('Prepare delivery', [report], context, execution_id='one')[0]
        rerun = run_code('Prepare delivery', [report], context, execution_id='two')[0]
        self.assertEqual(first['key'], rerun['key'])
        self.assertNotEqual(first['owner'], rerun['owner'])
        context['Delivery settings']['channelId'] = 'COTHER'
        other_channel = run_code('Prepare delivery', [report], context)[0]
        self.assertNotEqual(first['key'], other_channel['key'])
        report['report']['weekStart'] += 7 * 24 * 3600 * 1000
        other_week = run_code('Prepare delivery', [report], context)[0]
        self.assertNotEqual(other_channel['key'], other_week['key'])

    def test_bad_channel_fails_before_claim(self):
        with self.assertRaises(AssertionError):
            self.delivery(channel='REPLACE_WITH_SLACK_CHANNEL_ID')

    def claim(self, delivery=None, rows=None):
        delivery = delivery or self.delivery()
        row = {'id': 1, 'control_key': 'weekly-ratings', 'state': 'idle', 'owner': '',
               'active_key': '', 'revision': 0, 'ledger': '{}', 'payload': ''}
        out = run_code('Prepare claim', rows if rows is not None else [row], {'Prepare delivery': delivery})
        return out[0] if out else None

    def claimed_row(self, plan):
        return {'id': plan['controlId'], 'control_key': 'weekly-ratings', 'state': 'sending',
                'owner': plan['owner'], 'active_key': plan['key'], 'revision': plan['claimedRevision'],
                'ledger': plan['ledger'], 'payload': plan['storedPayload']}

    def test_guard_requires_matching_claim_owner_revision_and_content(self):
        plan = self.claim()
        row = self.claimed_row(plan)
        self.assertEqual(run_code('Build guarded request', [row], {'Prepare claim': plan}), [plan])
        for invalid in ({}, {'success': True}, {**row, 'state': 'idle'}, {**row, 'owner': 'other'},
                        {**row, 'active_key': 'other'}, {**row, 'revision': 0}, {**row, 'ledger': '{}'},
                        {**row, 'payload': 'different'}):
            self.assertEqual(run_code('Build guarded request', [invalid], {'Prepare claim': plan}), [])

    def test_missing_duplicate_and_corrupted_seed_fail_closed(self):
        seed = {'id': 1, 'control_key': 'weekly-ratings', 'state': 'idle', 'owner': '',
                'active_key': '', 'revision': 0, 'ledger': '{}', 'payload': ''}
        for rows in ([], [{}], [seed, {**seed, 'id': 2}], [{**seed, 'ledger': 'bad JSON'}],
                     [{**seed, 'ledger': '[]'}], [{**seed, 'revision': -1}],
                     [{**seed, 'ledger': '{"old": {"state": "unknown"}}'}]):
            with self.assertRaises(AssertionError):
                self.claim(rows=rows)
        with self.assertRaises(AssertionError):
            self.claim(rows=[{**seed, 'state': 'sending'}])

    def test_sent_ambiguous_and_unfinished_entries_block_but_failed_can_retry(self):
        delivery = self.delivery()
        for state in ('sent', 'sending', 'ambiguous', 'failed'):
            seed = {'id': 1, 'control_key': 'weekly-ratings', 'state': 'idle', 'owner': 'previous',
                    'active_key': delivery['key'], 'revision': 2, 'payload': '',
                    'ledger': json.dumps({delivery['key']: {'state': state, 'owner': 'previous', 'ts': '123.456' if state == 'sent' else None}})}
            plan = self.claim(delivery, [seed])
            if state == 'failed':
                self.assertEqual(plan['claimedRevision'], 3)
            else:
                self.assertIsNone(plan)

    def classify(self, response):
        return run_code('Classify delivery result', [response],
                        {'Build guarded request': self.delivery()})[0]

    def test_confirmed_success_and_explicit_rejection(self):
        result = self.classify({'statusCode': 200, 'body': {'ok': True, 'channel': 'CTEST', 'ts': '123.456'}})
        self.assertEqual((result['state'], result['ts']), ('sent', '123.456'))
        for status, error in ((200, 'channel_not_found'), (200, 'invalid_auth'), (429, 'ratelimited')):
            result = self.classify({'statusCode': status, 'body': {'ok': False, 'error': error}})
            self.assertEqual(result['state'], 'failed')
            self.assertIsNone(result['ts'])

    def test_ambiguous_outcomes_never_allow_blind_retry(self):
        for response in ({'error': 'timeout'}, {}, {'statusCode': 502, 'body': '<html>'},
                         {'statusCode': 200, 'body': {'ok': False, 'error': 'internal_error'}},
                         {'statusCode': 200, 'body': {'ok': False, 'error': 'unknown'}},
                         {'statusCode': 200, 'body': {'ok': True}},
                         {'statusCode': 200, 'body': {'ok': True, 'channel': 'COTHER', 'ts': '123.456'}}):
            self.assertEqual(self.classify(response)['state'], 'ambiguous')

    def test_only_owner_verified_persisted_success_completes(self):
        plan = self.claim()
        for state in ('sent', 'failed', 'ambiguous'):
            result = {'key': plan['key'], 'owner': plan['owner'], 'state': state,
                      'ts': '123.456' if state == 'sent' else None, 'detail': None}
            final = run_code('Prepare persistence', [result], {'Build guarded request': plan})[0]
            row = {**self.claimed_row(plan), 'state': 'idle', 'revision': final['completedRevision'],
                   'ledger': final['ledger']}
            if state == 'sent':
                out = run_code('Check delivery result', [row], {'Prepare persistence': final})
                self.assertEqual(out[0]['state'], 'sent')
                for invalid in ([], [{}], [{**row, 'owner': 'other'}], [{**row, 'revision': 0}],
                                [{**row, 'ledger': '{}'}], [row, row]):
                    with self.assertRaises(AssertionError):
                        run_code('Check delivery result', invalid, {'Prepare persistence': final})
            else:
                with self.assertRaises(AssertionError):
                    run_code('Check delivery result', [row], {'Prepare persistence': final})

    def test_invalid_completion_leaves_claim_locked(self):
        plan = self.claim()
        for result in ({'key': plan['key'], 'owner': 'other', 'state': 'sent', 'ts': '123.456'},
                       {'key': plan['key'], 'owner': plan['owner'], 'state': 'sent', 'ts': None},
                       {'key': plan['key'], 'owner': plan['owner'], 'state': 'failed', 'ts': '123.456'}):
            with self.assertRaises(AssertionError):
                run_code('Prepare persistence', [result], {'Build guarded request': plan})

    def test_export_wiring_has_no_delivery_bypass_or_retries(self):
        nodes = {n['name']: n for n in WF['nodes']}
        self.assertFalse(WF['active'])
        self.assertEqual(WF['connections']['Delivery enabled?']['main'][0][0]['node'], 'Read delivery state')
        self.assertEqual(WF['connections']['Post to Slack']['main'][0][0]['node'], 'Classify delivery result')
        incoming = [source for source, outputs in WF['connections'].items()
                    for branch in outputs['main'] for target in branch if target['node'] == 'Post to Slack']
        self.assertEqual(incoming, ['Build guarded request'])
        for name in ('Claim delivery', 'Post to Slack', 'Persist delivery result'):
            self.assertFalse(nodes[name]['retryOnFail'])
        post = nodes['Post to Slack']
        self.assertEqual(post['parameters']['options']['redirect']['redirect']['followRedirects'], False)
        self.assertNotIn('credentials', post)
        self.assertFalse(any(n['type'] == 'n8n-nodes-base.postgres' for n in WF['nodes']))
        for name in ('Claim delivery', 'Persist delivery result'):
            params = nodes[name]['parameters']
            self.assertEqual(params['operation'], 'update')
            self.assertEqual(params['matchType'], 'allConditions')
            self.assertEqual(params['options']['dryRun'], False)
            keys = {f['keyName'] for f in params['filters']['conditions']}
            self.assertTrue({'id', 'revision', 'state'} <= keys)
            if name == 'Persist delivery result':
                self.assertTrue({'owner', 'active_key'} <= keys)
        for n in WF['nodes']:
            if n['type'] == 'n8n-nodes-base.dataTable':
                self.assertEqual(n['parameters']['dataTableId']['value'], 'REPLACE_WITH_DATA_TABLE_ID')
