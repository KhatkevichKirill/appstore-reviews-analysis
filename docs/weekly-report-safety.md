# Weekly report: preview, delivery and recovery

Source baseline: `99b62d1200b486d53a57b77c23d323330c0afe49` (main on 2026-10-13).
This change implements AICP-175. It does not change the 48-hour freshest-value
heuristic, snapshot collection or written-review feed.

## Comparison rule

Both current and previous periods must have complete snapshot coverage and usable
rating deltas. Otherwise **both** `vs prev` columns show `—`, for the affected
storefront and for `All` when it includes that storefront. Missing data and a
count reset also suppress comparisons. A zero previous count has no percentage;
an absent or inconsistent mean has no average comparison. Complete-week math and
the table layout are unchanged. The footer explains suppressed partial comparisons.

Thus a report on October 12 for October 5–11 cannot compare its mean with the
partial October 1–4 baseline. BR/IN added mid-week also suppress their comparisons
and the combined comparison; complete storefronts remain independently eligible.
The Python `compare_periods` reference and exported JavaScript are tested together.

## Safe import and preview (no database or Slack needed)

1. Import `n8n/app-store-ratings-weekly-report.json` as a **new inactive workflow**.
   Do not replace or activate the production workflow as part of this fix.
2. Leave `Delivery settings.deliveryEnabled` as the boolean `false`.
3. Select the existing Google Sheets credential on both read nodes and replace
   spreadsheet placeholders. These nodes only read existing tabs.
4. Use **Manual preview** / Execute Workflow. Inspect `Build weekly report` or
   `Prepare delivery`: `slackBody` contains the rendered table; `report` contains
   current/previous numbers and comparisons. The delivery IF takes its false
   branch, so neither PostgreSQL nor Slack is executed. Missing delivery credentials
   are expected in preview. Do not execute downstream delivery nodes individually.

## Persistence requirements before any delivery is approved

Delivery needs an approved durable PostgreSQL database (9.5+), reachable from the
n8n execution workers, with the table from `sql/weekly-ratings-delivery.sql`. The
database must use durable storage with `fsync` and `synchronous_commit` enabled. The
schema is supplied for review; **nothing provisions or migrates a database**.
Do not use n8n's internal tables or workflow static data as the delivery journal.
Select an approved Postgres credential on both ledger nodes and the existing
Slack OAuth credential on the HTTP Request node. No secret or credential ID is
in the export. No additional Slack history scope is required by this workflow.
The role must be allowed to SELECT/INSERT/UPDATE the ledger. Setup or changes to
persistent access require separate approval.

Every workflow copy/worker posting these reports must share this ledger. Use the
same app ID and concrete Slack channel ID consistently. The key is
`app ID : Monday 00:00 UTC : channel ID`; execution IDs are attempt owners, not
keys. Changing a workflow name, restarting a worker or rerunning manually does not
create a new key. A different week or channel can claim independently. Keep the
ledger and its backups; deleting it loses duplicate protection.

The atomic PostgreSQL upsert commits a `sending` claim **before** the one Slack
request. The unique primary key excludes concurrent claimants, including concurrent
retries of `failed`. A loser stops without sending. There is no lease expiry or
automatic reclaim. Completion updates require the matching attempt owner.

The HTTP node has retries and redirects disabled, preserves HTTP status and Slack
`ok`, and uses a conservative rejection allowlist. The state machine is:

| State | Meaning | What another full workflow run does |
|---|---|---|
| `sending` | Claimed; may already have posted | Blocks |
| `sent` | Slack confirmed success; message timestamp saved | Blocks |
| `failed` | Explicit known rejection; no delivery confirmed | Atomically claims one retry |
| `ambiguous` | Timeout, malformed/unknown/server response, or unconfirmed success | Blocks |

A known rejection is never marked `sent`. Only HTTP 200 + `ok: true` + the expected
channel and a message timestamp is success. Failure or ambiguity stops the run
with a visible error after persistence. If a persistence node fails, keep the
execution evidence; `sending` remains blocked. Retrying **only the persistence
step** with the recorded classification is safe; retrying the Slack request is not.

## Recovery and rollout

Never retry/resume a failed execution at **Post to Slack**, replay its pinned
input, or configure automatic execution/node retries around that request. Start
ordinary retries at the workflow trigger so they must acquire a claim again.
Clear pinned data before enabling delivery. This is conservative at-most-once
attempt behavior, not a promise of exactly-once delivery: an uncertain attempt
may require manual recovery instead of sending automatically.

For stuck `sending`/`ambiguous`, first stop/cancel the owning execution and establish
that no worker can still send. Inspect its response and the destination channel.
If the message exists, record its Slack timestamp and change the ledger row to
`sent` under operator review. If non-delivery is definitively established, change
the row to `failed` and rerun from the trigger. If uncertain, leave it blocked.
Absence from a cursory history search or passage of time is not proof of failure.
Do not delete the row to force a resend.

Before switching from the old workflow, stop the old sender and seed `sent` rows
for reports already delivered to the chosen channel, using their original payload
and Slack timestamp. The new ledger cannot deduplicate unrecorded historical posts
or an old workflow still posting outside it. Do not run both senders in parallel.
No rollout is included in this PR. Activation and production posting remain subject
to separate approval after format review.

## Validation

`python3 -m unittest discover -s tests` runs computation/structure/hygiene regressions
and the exported Code nodes with mocked inputs. Node.js is required to avoid skipped
JavaScript checks. SQL integration checks execute the **actual exported queries**
in a local PostgreSQL WASM engine, with mocked delivery and persistent restart:

```sh
# Install only in a disposable validation directory, not the production environment.
npm install --prefix /tmp/ratings-validation @electric-sql/pglite
PGLITE_MODULE=/tmp/ratings-validation/node_modules/@electric-sql/pglite/dist/index.js \
  node tests/test_delivery_sql.mjs
```

This exercises the SQL engine's unique-key upsert and owner guards. PGlite serializes
local queries; separate-worker behavior relies on PostgreSQL's atomic
`INSERT ... ON CONFLICT` contract and must also be checked in an approved staging
n8n/PostgreSQL environment before activation. No live n8n import, real Slack send,
production writes or App Store Connect validation are part of these tests.

References: [n8n Postgres query parameters](https://docs.n8n.io/integrations/builtin/app-nodes/n8n-nodes-base.postgres/),
[Slack chat.postMessage response/errors](https://docs.slack.dev/reference/methods/chat.postMessage/),
[PostgreSQL atomic ON CONFLICT](https://www.postgresql.org/docs/current/sql-insert.html).
