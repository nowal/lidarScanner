# Quote request lifecycle and acceptance confirmation

One verified homeowner/home has one active intake. `POST /api/v1/ai/quote-intake/active`
restores that intake across installations/devices. Its receipt links to exactly one
`flow_quote_requests` record through the unique `quote_intakes.request_id` foreign
key. The existing provider-response foreign key is retained.

`POST /api/v1/ai/quote-intake/start-next` requires `homeId`, a fresh `conversationId`,
`previousConversationId`, and its accepted `quoteRequestId`. A transaction locks
the owner/home, verifies the receipt and persisted acceptance, creates the next
intake, and records the single successor. Concurrent devices and retries return
the saved active successor. Submitting, waiting, receiving bids, opening approval,
and declining do not authorize a transition. Acceptance does not start a new chat.

`flow_quote_acceptances` stores the immutable selected quote, method, validated
contact, signed-document reference and queued delivery state. A request-row lock
makes competing approvals choose once. Triggers preserve that snapshot through
stale parent writes and enforce intake/pointer owner/home associations. The
homeowner response exposes only the accepted quote after approval; other bids
remain stored. Signed links continue resolving through the original request and
keep their existing 30-day lifetime. Starting another chat does not delete data.

Provider/operations and homeowner confirmations use separate delivery claims,
receipts and stable transport idempotency keys. Each channel retries independently.
The homeowner recipient is the validated approval email, with no guessed or
profile-derived fallback. A callback includes submitted contact details; a signed
approval includes the private signed-copy link. Delivery failure leaves acceptance
and its queued messages durable. As with the existing mail transport, a crash after
an SMTP server accepts a message but before the receipt is saved cannot provide
exactly-once SMTP delivery; Resend receives a stable idempotency key.

## Upgrade and compatibility

Apply `supabase/migrations/20261030193724_quote_request_lifecycle.sql` before this
backend version. It adds private service-role tables/functions, backfills verified
receipt links and completed approvals, and selects the latest existing saved intake
per home. Backfill preserves existing provider delivery stamps and does not queue
surprise confirmations for historical approvals. No scan/bid/request/PDF is removed.
All new functions are security invoker with fixed search paths; public, anon and
authenticated execution/table access are revoked.

Previously saved uncertain intake submissions can finish their original payload
even if a later intake is active; their quotes remain on the historical request.
The active route can adopt the old direct client's pending submission identity;
its exact retry attaches the receipt to the active intake. Immutable retry payloads
and existing intake version/lease fencing are retained.

Coordinate rollout with the matching mobile changes. Older clients display the
new-request control prematurely and discard their local intake when it is tapped;
the new backend correctly rejects their fresh conversation, but those clients do
not know how to restore `/active`. **Do not deploy the strict lifecycle contract
alone while those clients are the only available app.** The new mobile version also
requires the new routes. Mobile main/TestFlight publication was authorized on October 30, 2026.
Prepare a coordinated app update/upgrade path before production cutover.

Production was only read during this implementation. TakeShapeApp schema and
receipt associations were inspected; there were no duplicate or mismatched intake
receipts. Existing security-advisor findings on unrelated tables/functions were
not changed. The migration was exercised in isolated PGlite, not applied live.

## Verification on October 30, 2026

- Full Python suite: 1,463 passed, 2 skipped. Outbound service credentials are
  disabled by the test harness; email transports are captured/mocked.
- Lifecycle API tests cover signed/callback acceptance, email content and separate
  failures, concurrent devices/workers, late historical bids, original signed links,
  legacy pending recovery, receipt ownership, and independent notification receipts.
- PostgreSQL tests cover migration/backfill, private grants, request membership,
  immutable acceptance, owner/home links, successor retry semantics, CAS fencing,
  and independent delivery claims. These use PostgreSQL via PGlite, not a live
  PostgREST integration or production customer data.

Run from the repository root:

```sh
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests -q
PGLITE_MODULE=/path/to/@electric-sql/pglite node backend/tests/sql/quote_request_lifecycle.cjs
PGLITE_MODULE=/path/to/@electric-sql/pglite node backend/tests/sql/quote_intake_state.cjs
```

Before release, recheck the deployed Render SHA and migration state, apply the
additive migration, deploy the backend with the coordinated mobile rollout, and
verify both approval methods using isolated accounts and a captured email sink.
Physical-device end-to-end delivery and real PostgREST concurrency remain release
checks; fixture simulator/local database coverage does not establish those results.

## Authorized release — October 30, 2026

The lifecycle migration was applied to TakeShapeApp as version `20261030193724`. The migration file now uses the database-assigned version to keep local and hosted history aligned. It retained all eight intakes, linked five receipts, selected seven active homes, and backfilled two accepted requests without queueing historical homeowner messages. Real PostgreSQL transaction checks passed for active recovery, acceptance gating, immutable acceptance, successor retries, private privileges, and independent delivery claims; every synthetic row was rolled back. Backend and mobile rollout is proceeding under the user’s explicit deployment authorization.
