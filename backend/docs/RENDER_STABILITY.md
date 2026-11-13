# Render storage and idle traffic fix

Render recorded `/tmp` 2 GB evictions on November 9 and 13, 2026. The
service also produced a steady 45–50 MB/hour of outbound traffic. The live
database contained 79 quote requests; the email recovery workers hydrated
every request, including two related-record reads per request, on each pass.

## Behavior

- On Render (`RENDER=true`, provided by the platform), application logs go to
  the console only. Render retains those logs; the app no longer duplicates
  them into `storage/logs/server.log` and `server.jsonl` under `/tmp`.
- Outside Render, each local log rotates at 5 MiB with three backups.
- HTTPX/httpcore INFO chatter is suppressed; warnings/errors and application
  logs remain available.
- Lead recovery queries only queued, undelivered, submitted records and
  hydrates only those candidates. Surviving legacy lead outbox files remain
  recoverable. Existing status eligibility is preserved.
- Acceptance recovery joins pending immutable delivery receipts to their
  parent requests. A separate anti-join finds older approved requests without
  receipts. Delivered receipts override stale parent fields.
- Capture-only operation, per-recipient retries, existing claims and
  idempotency keys, and the 30/60-second polling intervals are unchanged.
- Failed production discovery queries raise instead of sending from stale
  local cached records. The existing worker loops retry on their next pass.

With no eligible deliveries, each lead pass uses one filtered query and each
acceptance pass uses two. Query count no longer grows by two requests for
every historical quote. Backlogs are paginated before deliveries begin.

There are no schema, API, client, model, compute-plan, or billing changes.
This release is based directly on `dbb2e68` and does not include the separate
restore/latency PR.

## Validation

Run from `backend` with the project's Python environment:

```sh
python -m pytest -q tests/test_email_recovery.py tests/test_logging_setup.py tests/test_ops_email.py tests/test_quote_lifecycle.py
python -m pytest -q
```

The focused tests cover filtered idle queries, pagination, discovery failure,
authoritative receipts, partial delivery retries, captured mail, legacy
approvals/outbox files, late bids, rotation, and console-only Render logging.
Production database validation uses read-only aggregate queries; tests do not
send email or mutate production records.

Three existing email-copy assertions fail on the base revision as well:
`test_quintin_oct6_rubrics::test_the_lead_package_carries_the_fields_the_scan_and_the_unknowns`,
`test_quote_decisions_sep17::test_operations_is_told_about_an_approval_with_the_address`,
and `test_scan_scope::test_the_email_says_one_room_or_whole_home_and_still_withholds_the_address`.

## Rollout verification

After the new commit is live, verify `/health`, runtime logs, and recovery
query execution. Render bandwidth data arrives hourly with an additional
delay; an immediate health check cannot establish the final bandwidth saving
or long-term absence of storage evictions. No new disk or larger compute plan
is required. A normal Render deploy starts with fresh ephemeral storage.
