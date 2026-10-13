"""Local SQLite model of Data Table conditional updates + exported Code nodes.

This is not a live n8n runtime test. The adapter builds the SQL WHERE and mapped
values from the exported node parameters, then verifies races on a real local DB.
It models n8n's SQLite reread behavior, including a lost update returning a row.
"""
import csv
from contextlib import contextmanager
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import test_weekly_safety as safety
from test_workflows import NODE, ROOT

NODES = {n['name']: n for n in safety.WF['nodes']}


def evaluate_parameters(name, data):
    src = '''const [params, data] = process.argv.slice(1).map(JSON.parse);
const evaluate = value => {
  if (typeof value === 'string' && value.startsWith('={{'))
    return new Function('$json', 'return (' + value.slice(3, -2) + ');')(data);
  if (Array.isArray(value)) return value.map(evaluate);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k,v]) => [k,evaluate(v)]));
  return value;
};
console.log(JSON.stringify(evaluate(params)));'''
    out = subprocess.run([NODE, '-e', src, json.dumps(NODES[name]['parameters']), json.dumps(data)],
                         capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class SQLiteDataTable:
    def __init__(self, db_file, seed=False):
        self.db_file = db_file
        if seed:
            with self.connect() as db:
                db.execute('CREATE TABLE state (id INTEGER PRIMARY KEY, control_key TEXT, state TEXT, '
                           'owner TEXT, active_key TEXT, revision INTEGER, ledger TEXT, payload TEXT)')
                with open(os.path.join(ROOT, 'templates', 'weekly-ratings-delivery-seed.csv'), newline='') as f:
                    row = next(csv.DictReader(f))
                columns = list(row)
                db.execute('INSERT INTO state (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')',
                           list(row.values()))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_file, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def read(self):
        with self.connect() as db:
            # Exported Get has a two-row limit so duplicate seeds are detected.
            params = NODES['Read delivery state']['parameters']
            return [dict(r) for r in db.execute('SELECT * FROM state WHERE control_key = ? LIMIT ?',
                    (params['filters']['conditions'][0]['keyValue'], params['limit']))]

    def update(self, name, data, previously_selected_ids=None):
        params = evaluate_parameters(name, data)
        assert params['operation'] == 'update' and params['matchType'] == 'allConditions'
        conditions = params['filters']['conditions']
        assert all(c['condition'] == 'eq' for c in conditions)
        allowed = {'id', 'control_key', 'state', 'owner', 'active_key', 'revision', 'ledger', 'payload'}
        assert all(c['keyName'] in allowed for c in conditions)
        values = params['columns']['value']
        assert set(values) <= allowed - {'id'}
        where = ' AND '.join('"' + c['keyName'] + '" = ?' for c in conditions)
        binds = [c['keyValue'] for c in conditions]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            ids = previously_selected_ids
            if ids is None:
                ids = [r['id'] for r in db.execute('SELECT id FROM state WHERE ' + where, binds)]
            # Apply conditions to the UPDATE itself, not only to a prior SELECT.
            db.execute('UPDATE state SET ' + ','.join('"' + k + '" = ?' for k in values) + ' WHERE ' + where,
                       list(values.values()) + binds)
            result = [dict(r) for r in db.execute('SELECT * FROM state WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids)] if ids else []
            return result


@unittest.skipUnless(NODE, 'node is not installed')
class DataTableDeliveryIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_file = os.path.join(self.temp.name, 'table.sqlite')
        self.table = SQLiteDataTable(self.db_file, seed=True)
        self.posts = []

    def delivery(self, owner='one', week=1791158400000, channel='CTEST'):
        return safety.run_code('Prepare delivery', [{'report': {'weekStart': week},
            'slackBody': {'text': 'mocked report', 'blocks': []}}],
            {'Delivery settings': {'deliveryEnabled': True, 'appId': '1209815023', 'channelId': channel}},
            execution_id=owner)[0]

    def plan(self, delivery, rows=None):
        return safety.run_code('Prepare claim', self.table.read() if rows is None else rows,
                               {'Prepare delivery': delivery})

    def guard(self, plan, rows):
        return safety.run_code('Build guarded request', rows, {'Prepare claim': plan})

    def claim(self, delivery):
        plans = self.plan(delivery)
        if not plans:
            return None
        plan = plans[0]
        rows = self.table.update('Claim delivery', plan)
        guarded = self.guard(plan, rows)
        return guarded[0] if guarded else None

    def final_plan(self, claim, response):
        result = safety.run_code('Classify delivery result', [response], {'Build guarded request': claim})[0]
        return safety.run_code('Prepare persistence', [result], {'Build guarded request': claim})[0]

    def finish(self, claim, response):
        final = self.final_plan(claim, response)
        rows = self.table.update('Persist delivery result', final)
        return final, rows

    def success(self, claim):
        return {'statusCode': 200, 'body': {'ok': True, 'channel': claim['channelId'], 'ts': '123.456'}}

    def test_eight_concurrent_attempts_only_one_guarded_mock_send(self):
        initial = self.table.read()
        plans = [self.plan(self.delivery(owner='race-' + str(i)), initial)[0] for i in range(8)]
        barrier = Barrier(8)
        def attempt(plan):
            barrier.wait(timeout=10)
            # All contenders saw id 1 before the update; simulate SQLite's later
            # reread returning that id even when their WHERE no longer matches.
            rows = self.table.update('Claim delivery', plan, previously_selected_ids=[1])
            return self.guard(plan, rows)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, plans))
        self.posts = [r[0]['payload'] for r in results if r]
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.table.read()[0]['revision'], 1)
        self.assertEqual(self.table.read()[0]['state'], 'sending')

    def test_sent_rerun_blocked_but_different_week_and_channel_eligible(self):
        first = self.claim(self.delivery())
        self.posts.append(first['payload'])
        final, rows = self.finish(first, self.success(first))
        self.assertEqual(safety.run_code('Check delivery result', rows, {'Prepare persistence': final})[0]['state'], 'sent')
        self.assertIsNone(self.claim(self.delivery(owner='rerun')))
        self.assertEqual(len(self.posts), 1)
        for delivery in (self.delivery('next-week', week=1791763200000), self.delivery('other-channel', channel='COTHER')):
            claim = self.claim(delivery)
            self.assertIsNotNone(claim)
            self.finish(claim, self.success(claim))
        # Updating the singleton preserves all historical keys, not only the last.
        self.assertEqual(len(json.loads(self.table.read()[0]['ledger'])), 3)
        self.assertIsNone(self.claim(self.delivery(owner='old-week-rerun')))

    def test_failed_send_has_no_success_marker_and_only_one_retry_claim(self):
        first = self.claim(self.delivery())
        final, rows = self.finish(first, {'statusCode': 200, 'body': {'ok': False, 'error': 'channel_not_found'}})
        entry = json.loads(rows[0]['ledger'])[first['key']]
        self.assertEqual((entry['state'], entry['ts']), ('failed', None))
        initial = self.table.read()
        a = self.plan(self.delivery('retry-a'), initial)[0]
        b = self.plan(self.delivery('retry-b'), initial)[0]
        self.assertTrue(self.guard(a, self.table.update('Claim delivery', a)))
        self.assertEqual(self.guard(b, self.table.update('Claim delivery', b, previously_selected_ids=[1])), [])
        with self.assertRaises(AssertionError):
            safety.run_code('Check delivery result', rows, {'Prepare persistence': final})

    def test_timeout_persisted_blocks_same_report_and_releases_for_other_weeks(self):
        first = self.claim(self.delivery())
        self.finish(first, {'error': 'timeout'})
        self.assertEqual(self.table.read()[0]['state'], 'idle')
        self.assertIsNone(self.claim(self.delivery('blind-retry')))
        self.assertIsNotNone(self.claim(self.delivery('other-week', week=1791763200000)))

    def test_crash_after_send_before_persist_survives_database_reopen(self):
        first = self.claim(self.delivery())
        self.posts.append(first['payload'])
        # Reopening a local persistent file models restart, not a cleared store.
        self.table = SQLiteDataTable(self.db_file)
        with self.assertRaises(AssertionError):
            self.claim(self.delivery('after-restart'))
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.table.read()[0]['active_key'], first['key'])
        self.assertEqual(json.loads(self.table.read()[0]['payload'])['text'], 'mocked report')

    def test_persistence_failure_leaves_blocked_claim_and_can_retry_only_persistence(self):
        first = self.claim(self.delivery())
        final = self.final_plan(first, self.success(first))
        with self.assertRaises(AssertionError):
            self.claim(self.delivery('retry-whole-workflow'))
        rows = self.table.update('Persist delivery result', final)
        self.assertEqual(safety.run_code('Check delivery result', rows, {'Prepare persistence': final})[0]['state'], 'sent')
        # Replaying the completion update can never acquire or send again.
        self.assertEqual(self.table.update('Persist delivery result', final), [])
        self.assertIsNone(self.claim(self.delivery('another-retry')))

    def test_stale_owner_and_revision_cannot_overwrite_later_week(self):
        first = self.claim(self.delivery('old-owner'))
        stale = self.final_plan(first, self.success(first))
        self.table.update('Persist delivery result', stale)
        later = self.claim(self.delivery('new-owner', week=1791763200000))
        rows = self.table.update('Persist delivery result', stale, previously_selected_ids=[1])
        with self.assertRaises(AssertionError):
            safety.run_code('Check delivery result', rows, {'Prepare persistence': stale})
        self.assertEqual(self.table.read()[0]['owner'], later['owner'])
        self.assertEqual(self.table.read()[0]['state'], 'sending')
        self.assertEqual(len(json.loads(self.table.read()[0]['ledger'])), 2)

    def test_duplicate_seed_and_missing_seed_never_reach_send(self):
        with self.table.connect() as db:
            db.execute('INSERT INTO state (control_key, state, owner, active_key, revision, ledger, payload) '
                       "VALUES ('weekly-ratings','idle','','',0,'{}','')")
        with self.assertRaises(AssertionError):
            self.claim(self.delivery())
        with self.table.connect() as db:
            db.execute('DELETE FROM state')
        with self.assertRaises(AssertionError):
            self.claim(self.delivery())
        self.assertEqual(self.posts, [])
