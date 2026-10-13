# Weekly report: preview, Data Table delivery and recovery

Source baseline: `99b62d1200b486d53a57b77c23d323330c0afe49` (main on 2026-10-13).
This change implements AICP-175. Snapshot collection, the written-review feed and
the 48-hour freshest-value heuristic are unchanged.

## Comparison rule

Both current and previous periods must have complete snapshot coverage and usable
rating deltas. Otherwise **both** `vs prev` columns show `—`, for the affected
storefront and for `All` when it includes that storefront. Missing data and a
count reset also suppress comparisons. A zero previous count has no percentage;
an absent or inconsistent mean has no average comparison. Complete-week math and
the table layout are unchanged. The footer explains suppressed partial comparisons.

An October 12 report for October 5–11 cannot compare its mean with the partial
October 1–4 baseline. BR/IN added mid-week suppress their comparisons and the
aggregate comparison; complete storefronts remain independently eligible.
The Python `compare_periods` reference and exported JavaScript are tested together.

## Safe import and preview

1. Import `n8n/app-store-ratings-weekly-report.json` as a **new inactive workflow**.
   Do not replace or activate the production workflow as part of this fix.
2. Leave `Delivery settings.deliveryEnabled` as the boolean `false`.
3. Select the existing Google Sheets credential on both read nodes and replace
   spreadsheet placeholders. These nodes only read existing tabs.
4. Use **Manual preview** / Execute Workflow. Inspect `Build weekly report` or
   `Prepare delivery`: `slackBody` contains the rendered table; `report` contains
   current/previous numbers and comparisons. The IF takes its false branch, so
   neither the Data Table nor Slack is accessed. No delivery table is needed for
   preview. Do not execute downstream delivery nodes individually.

## One built-in Data Table, no external database

Delivery uses only the supported **Data Table** node, version 1, inside n8n.
There is no SQL node, external PostgreSQL service, extra database credential,
manual access to n8n's internal tables, or workflow static-data dependency.

Before separately approved delivery setup, create a dedicated Data Table named
`Weekly ratings delivery` **in the workflow's project** with these columns:

| Column | Type | Purpose |
|---|---|---|
| `control_key` | string | Constant `weekly-ratings` |
| `state` | string | `idle` or `sending` |
| `owner` | string | Execution ID of the latest attempt |
| `active_key` | string | Latest app/week/channel key |
| `revision` | number | Monotonically increasing change counter |
| `ledger` | string | JSON history of report keys and delivery results |
| `payload` | string | JSON of the latest Slack payload, useful for recovery |

Import `templates/weekly-ratings-delivery-seed.csv` into that table **once**, or manually
add its one row. Use the column types above. The seed uses nonempty strings to
avoid CSV import converting empty cells to null. Do not create an `id` column:
n8n assigns the native row ID. Do not import the seed again or create extra rows.
The workflow refuses to send if the control row is missing, duplicated or invalid.

Select this same table in **Read delivery state**, **Claim delivery**, and
**Persist delivery result**. Replace the Slack channel placeholder in Delivery
settings and select the existing Slack OAuth credential on the HTTP Request node.
Leave delivery disabled until preview, review and isolated runtime tests pass.
Nothing in the export creates a table, changes credentials or activates a workflow.

All copies/instances of the sender in this n8n project must use the **same table**.
Back up the table along with n8n. Deleting/reseeding it or discarding `ledger` loses
the historical duplicate protection. A separate n8n instance with a separate table
has independent state and must not send the same reports to the same channel.

## Conditional claim, rather than check-then-insert

The one seeded row contains a small JSON ledger. Report keys are
`app ID : Monday 00:00 UTC : concrete Slack channel ID`. Execution IDs identify
attempt owners, not report keys. The ledger retains previous weeks and channels;
only the latest payload is stored, so history does not accumulate full messages.

A sender reads the idle row and its revision, checks the key's ledger status, and
performs **Update** with **All Conditions**: native row ID, observed revision and
`state = idle`. That same UPDATE writes `sending`, its owner, active key, next
revision, updated ledger and payload **before** Slack is called. No Insert/Upsert
is performed at run time, so there is no competing first-insert race.

The request guard validates the returned owner, row ID, key, revision, ledger and
payload. Merely receiving a row from Update is insufficient: n8n's SQLite path
can reread previously selected IDs after an update. Losing/stale contenders stop.
Completion uses another conditional Update: row ID, claimed revision, `sending`,
owner and active key must all match. The resulting ledger and revision are checked
before treating completion as successful. Missing completion output is an error.

The singleton serializes attempts across destinations. A competing attempt stops;
it can be run again from the trigger after the owner finishes. A crashed owner
leaves the row locked and blocks **all** deliveries using this table until reviewed.
There is no lock expiry or automatic reclaim; this deliberately favors avoiding
an uncertain duplicate over unattended recovery.

| Ledger state for a report key | Meaning | Subsequent full workflow run |
|---|---|---|
| `sending` | Attempt may already have posted | Blocks |
| `sent` | Slack confirmed success; timestamp saved | Blocks |
| `failed` | Explicit known rejection | Eligible for one new conditional claim |
| `ambiguous` | Timeout, unknown/server response or unconfirmed success | Blocks |

Only HTTP 200 + `ok: true` + expected channel + message timestamp is success.
Known rejections are not marked `sent`. Unknown outcomes are blocked. The HTTP
node has retries and redirects disabled. A persisted failure/ambiguity releases
the global row but blocks or permits that specific key as above; other keys remain
eligible. Failures are surfaced as execution errors after persistence.

## Recovery and rollout

Never retry/resume **Post to Slack**, replay pinned input, or configure automatic
retries around that request. Ordinary retries start at the workflow trigger and
must acquire a fresh claim. Clear pinned data before enabling delivery.
This is conservative at-most-once attempt behavior, not guaranteed delivery.

If completion persistence fails, the control row stays `sending`. Retrying **only
the persistence step** using its recorded classification is safe; resending Slack
is not. Before manually recovering a stuck row or an `ambiguous` entry, stop its
owning execution and establish that no worker can still send. Inspect the recorded
payload/response and the destination channel. If delivered, record its Slack
message timestamp as `sent`. Only definitively established non-delivery may be
changed to `failed`. If uncertain, leave it blocked. A cursory history search or
elapsed time does not prove non-delivery.

Recovery edits must preserve other ledger entries. When releasing a stuck control
row, also increment `revision` and set `state` to `idle`; this prevents stale
attempts from updating the recovered state. Do not erase the ledger to force a send.

Before switching, stop the old sender and add `sent` ledger entries for already
delivered reports in the chosen channel (with their actual Slack timestamp and an
owner such as `migration`). The table cannot deduplicate unrecorded historical
posts or an old sender outside this guard. Never run both senders in parallel.
No production rollout or activation is included in this PR.

## Validation and limits

Run `python3 -m unittest discover -s tests`. Node.js is required to avoid skipping
exported-Code-node checks; no additional package installation is needed.

Tests execute the exported Code nodes and render the Data Table update conditions
and mapped values from the actual exported parameters. Integration tests use a
local SQLite adapter with independent connections and real conditional UPDATEs.
Eight competing attempts share a stale initial read; only one passes the request
guard. Tests deliberately model SQLite returning a reread row to losing callers,
and cover successful reruns, failed retries, uncertainty, stale owners/revisions,
missing/duplicate seeds, preserved history and durable reopen after a mocked send.

Read-only connected n8n checks on 2026-10-13 confirmed existing Data Tables and
obtained the Data Table Get/Update node schemas. Every node configuration and an
SDK representation of the full graph were validated. This establishes schema and
graph compatibility, **not** a live runtime concurrency test or an exact server
release identification. Repository update behavior was checked against current
upstream source; local SQLite tests model it rather than running the n8n service.

Subsequent explicitly authorized tests on 2026-10-13 created one isolated workflow
and one isolated Data Table on the connected n8n instance. The exported claim,
request guard, classification and persistence Code nodes were used unchanged,
with synthetic reports and persistent mock Slack receipts. Four conditional
updates overlapped and only one sent. Repeats, definite failure and retry,
uncertainty, interruption after a mock send, and scoped recovery all passed.
See the [runtime evidence and limitations](isolated-n8n-validation.md).
A separately authorized follow-up sent one synthetic message to the designated
test Slack channel using the production HTTP sender parameters and an existing
n8n OAuth credential. Slack confirmed success, completion was persisted, and
the repeat stopped before claim. Channel history contained zero matching test
messages before sending and exactly one after the repeat. The fixture is inactive,
unpublished and defaults to observation; its real HTTP sender is disabled again.
No production workflow/table was changed. Manual/scheduled trigger overlap and
physical server restart remain untested; production activation still requires
separate approval.

References: [Data Tables](https://docs.n8n.io/data/data-tables/),
[Data Table Update](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.datatable/rows/),
[update-node implementation](https://github.com/n8n-io/n8n/blob/master/packages/nodes-base/nodes/DataTable/actions/row/update.operation.ts),
[conditional update and SQLite reread](https://github.com/n8n-io/n8n/blob/master/packages/cli/src/modules/data-table/data-table-rows.repository.ts),
[Slack chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/).
