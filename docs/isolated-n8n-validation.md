# Isolated n8n Data Table validation

On 2026-10-13, an explicitly authorized isolated test ran on the connected n8n
instance against source revision `c6a6083d93d66bf6ea9eed6b9ed727a5d433ecc3`.
Only a newly created test workflow and test Data Table were written. Production
workflows and tables were outside the test scope.

The fixture uses the production export's `Prepare delivery`, `Prepare claim`,
`Build guarded request`, `Classify delivery result`, `Prepare persistence` and
`Check delivery result` Code sources unchanged. Data Table Get/Update settings
retain their conditions and mappings but target the new table. Reports, app ID,
channels and message timestamps are synthetic. Instead of calling Slack, a native
Data Table Insert records a mock receipt before a Code node returns a mock response.
The fixture has no credentials, Slack nodes, HTTP nodes or automatic triggers.

## Observed results

| Scenario | Result |
| --- | --- |
| Two manual attempts reading revision 0 before either claim | One receipt; the losing conditional update returned no rows |
| Repeat of a confirmed send | Stops before claim; no additional receipt |
| Four attempts reading revision 2, released at a shared barrier | Four overlapping conditional updates; one receipt and one confirmed completion |
| Repeat after the four-way race | Stops before claim; no additional receipt |
| Different week, same channel | Eligible; one receipt; history preserved |
| Same week, different channel | Eligible; one receipt; history preserved |
| Definite Slack rejection (`ok:false`, `channel_not_found`) | No receipt; `failed` persisted; run reports an unconfirmed delivery error |
| Retry of the definite rejection | Eligible; one receipt; then repeated run suppressed |
| Timeout after mock receipt | One receipt; `ambiguous` persisted; repeated run suppressed |
| Simulated exception after mock receipt, before completion | One receipt; control row remains `sending`; repeated run errors before claim |
| Recovery of the stopped, simulated crashed owner | Conditional update records `sent` using its single persisted mock receipt; next repeat suppressed |

For the four-way race, all attempts prepared the same observed revision before
the barrier at **16:22:04 UTC**. Claim nodes started at `.003`, `.043`, `.057`
and `.078` seconds and took **311, 289, 316 and 319 ms**, respectively. Their
update intervals overlapped; the winner set revision 3 and completed at revision 4.
The three losing attempts returned zero rows and never reached the mock sender.

Final persisted readback contains **seven receipts for seven distinct keys, one
per key**, with six `sent` entries and one `ambiguous` entry. The control row is
`idle` at revision 16. The recovery retained all earlier ledger entries. Expected
error executions in the rejection/uncertainty/crash scenarios are deliberate
signals from the conservative guard, not unexpected test failures.

## Scope and limitations

These results demonstrate conditional claim and completion behavior on the tested
n8n instance. They do not prove universal behavior across n8n releases, database
backends or deployments. Four manual executions overlapped at the update operation;
scheduled-trigger overlap was not exercised. No real Slack request or physical
server restart was performed. Mock recovery is based on a stored synthetic receipt;
real recovery still requires checking the actual Slack destination and stopped owner.

The fixture remains inactive and unpublished, with its default `observe` mode
reading the test table. Evidence is retained for review. The production export
remains inactive with `deliveryEnabled:false`. The existing sender must be retired
and delivered history seeded before a separately approved rollout.
