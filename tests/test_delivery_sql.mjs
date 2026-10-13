// Optional integration check: execute the exported SQL in a local PostgreSQL
// WASM engine. No Slack calls or external database connections are made.
// PGLITE_MODULE=/absolute/path/to/pglite/dist/index.js node tests/test_delivery_sql.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
const { PGlite } = await import(process.env.PGLITE_MODULE
  ? pathToFileURL(process.env.PGLITE_MODULE).href : '@electric-sql/pglite');
const root = fileURLToPath(new URL('../', import.meta.url));
const wf = JSON.parse(fs.readFileSync(path.join(root, 'n8n/app-store-ratings-weekly-report.json')));
const sql = name => wf.nodes.find(n => n.name === name).parameters.query;
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ratings-ledger-test-'));
let db;
let checks = 0;
try {
  db = new PGlite(dir);
  await db.exec(fs.readFileSync(path.join(root, 'sql/weekly-ratings-delivery.sql'), 'utf8'));
  const claim = (key, owner) => db.query(sql('Claim delivery'), [key, owner, JSON.stringify({text: 'mock report'})]);
  const finish = (key, owner, state, ts = null, detail = null) =>
    db.query(sql('Persist delivery result'), [key, owner, state, ts, detail]);
  const key = '1209815023:2026-10-05T00:00:00.000Z:CTEST';
  // Racing attempts: only one atomic insert/upsert wins. Losing runs get no
  // claim and therefore cannot pass Build guarded request.
  const racing = await Promise.all(Array.from({length: 12}, (_, i) => claim(key, 'race-' + i)));
  assert.equal(racing.filter(r => r.rows.length === 1).length, 1); checks++;
  const winner = racing.find(r => r.rows.length).rows[0].owner;
  let mockedPosts = 0;
  for (const r of racing) if (r.rows.length) mockedPosts++;
  assert.equal(mockedPosts, 1); checks++;
  assert.equal((await finish(key, 'loser', 'sent', '100.1')).rows.length, 0); checks++;
  await finish(key, winner, 'sent', '100.1');
  assert.equal((await claim(key, 'rerun')).rows.length, 0); checks++;
  assert.equal((await claim(key.replace('CTEST', 'COTHER'), 'other-channel')).rows.length, 1); checks++;
  assert.equal((await claim(key.replace('2026-10-05', '2026-10-12'), 'other-week')).rows.length, 1); checks++;
  // Explicit Slack rejection permits a new attempt; stale owners cannot finish it.
  const failedKey = key + ':failed';
  await claim(failedKey, 'first');
  await finish(failedKey, 'first', 'failed', null, 'channel_not_found');
  const retries = await Promise.all([claim(failedKey, 'retry-a'), claim(failedKey, 'retry-b')]);
  assert.equal(retries.filter(r => r.rows.length).length, 1); checks++;
  assert.equal((await finish(failedKey, 'first', 'sent', '100.2')).rows.length, 0); checks++;
  // Unknown outcome remains blocked; no expiry-based reclaim.
  const unknownKey = key + ':unknown';
  await claim(unknownKey, 'unknown');
  await finish(unknownKey, 'unknown', 'ambiguous', null, 'timeout');
  assert.equal((await claim(unknownKey, 'blind-retry')).rows.length, 0); checks++;
  // Crash after mock send but before persistence: durable sending record blocks.
  const crashKey = key + ':crash';
  await claim(crashKey, 'crashed');
  mockedPosts++;
  await db.close();
  db = new PGlite(dir);
  assert.equal((await claim(crashKey, 'after-restart')).rows.length, 0); checks++;
  assert.equal((await claim(key, 'after-restart')).rows.length, 0); checks++;
  // Ledger refuses invalid success records and is parameterized even for quotes.
  await assert.rejects(finish(crashKey, 'crashed', 'sent', null)); checks++;
  const quoted = "app:week:C'quoted; DROP TABLE weekly_ratings_delivery;--";
  assert.equal((await claim(quoted, 'quoted')).rows[0].report_key, quoted); checks++;
  console.log(`${checks} SQL integration checks passed (local PGlite, mocked delivery).`);
} finally {
  if (db) await db.close();
  fs.rmSync(dir, {recursive: true, force: true});
}
