# Quote intake implementation and rollout

Implemented October 19, 2026 in `takeshape-backend-staged-upload` and the active
`takeshape-mobile` checkout. The legacy home-guide routes, prompts, schema,
rubrics, conversation state and global provider selection remain independent.
Existing mobile changes in the working tree were retained.

## Runtime and API

- `app/quote_intake/` owns the strict schema, rubric loader, text-only Anthropic
  adapter, validation/readiness, durable state and versioned API.
- `POST /api/v1/ai/quote-intake/turn`: `conversationId`, `homeId`, `clientTurnId`
  (UUIDs), `expectedRevision`, verbatim `text`, IANA `timeZone`.
- `GET /api/v1/ai/quote-intake/{conversationId}?homeId=...`: recovery, complete
  transcript, result, pending turn, revision and receipt.
- `POST /api/v1/ai/quote-intake/submit`: `conversationId`, `homeId`,
  `expectedRevision`, `uploadRevision`. It reuses the direct endpoint deliberately;
  no legacy chat turn or flow token is synthesized.

Every route independently verifies the guest/account token and scan ownership.
Conversation storage checks homeowner AND scan, then uses a versioned
compare-and-swap lease for single flight across workers. The accepted homeowner
turn is durable before Claude is called. A failed response preserves that turn;
retry with its unchanged ID/body. Completed retries return the latest state,
which prevents an old retry from rolling back subsequent answers. A different
payload with the same turn ID is rejected. Revision conflicts never overwrite
state. A crashed worker's lease expires; stale responses cannot commit.

Claude receives text only: this intake's full transcript (up to 100 homeowner
turns, never an eight-message window), structured cited answers, supported
catalog labels, the pinned rubric and minimal workflow facts. All service entries
are eligible during routing, including corrections to a different service.
No home index, geometry, photo, scan description, old-agent context, room key,
media URL or credential is passed. Homeowner-volunteered facts are still text
input. Each answer cites an exact span and source turn. These checks establish
provenance; live semantic quality still requires evaluation.

The application, rather than model prose, owns scope/ZIP readiness and actual
submission. Unknown physical facts are provider-review notes. Permitted
uncertainty satisfies a decision without becoming a fabricated preference.
Conditional decisions use enumerated structured conditions; no expression is
executed. Multiple work items stay separate; unsupported services have a null
catalog mapping and human routing review. The provider package includes the
original request, current cited answers, unresolved choices, exclusions/logistics,
ZIP and prompt/rubric hashes. The model's freeform brief is available in state;
the delivered brief is assembled from cited answers and rubric review notes.
Freeform scope is quoted from homeowner evidence in the provider-facing brief;
only rubric-enumerated choices use their normalized values there. Structured
state retains the model's interpretation beside its source citation for audit.

Prompt revision `quote-intake-v1.1` clarifies the wire contract based on live
Claude results: extract common and service fields together, omit unasked fields,
use null values for deferrals, preserve unchanged cited answers, and retain
legitimate scope even in a message containing attempted instruction overrides.
The rubric still supplies every service rule. The endpoint and live evaluator
share `conversation.generate_turn`, including the same two-attempt validation
limit. A valid proposed question uses the remaining attempt to check whether
the transcript already supplies its answer, without inventing a missing choice.
Corrective regeneration receives the invalid candidate and specific
field error, so supported answers are not lost during repair. Model-proposed
completion with omitted required fields is repaired before an unnecessary
fallback question is shown. Genuine missing decisions remain questions.

The direct payload's optional `intakeDetails` field carries this structured
package. Omitting it preserves the old request fingerprint. Intake requests use
`described_scope` when attaching the full scan, so attachment scope does not
assert whole-home work. No room-name-to-ID guesses are made. Existing snapshot,
operations delivery, quote polling, PDF, signing/callback and contact-release
behavior is reused. Contact collection stays at acceptance.

Before dispatch, a new turn invalidates readiness. Pause/cancel/browse disables
dispatch, and only explicit resumption reopens it. Once dispatch starts, the
pending body and submission ID are frozen across uncertain retries. Recover the
receipt before starting a new request; never mutate a sent request. Upload
rejections can release a queued payload; a lost success response cannot.

## Editing the service guide

1. Edit the YAML blocks in `QUOTE_INTAKE_RUBRICS.md`. That document is the single
   authored service definition source. Change questions, priorities, acceptable
   deferrals, conditions and provider-review notes there.
2. From the repository root run:

   ```sh
   PYTHONPATH=backend .venv/bin/python -m app.quote_intake.rubrics
   PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_quote_intake.py -q
   PYTHONPATH=backend .venv/bin/python backend/scripts/evaluate_quote_intake.py
   ```

3. Resolve every validation error. Unknown YAML keys, duplicate keys/IDs,
   invalid types, invalid catalog labels, invalid condition references/values,
   cyclic conditions, or missing core fields fail loudly. Do not remove a
   required field to bypass a broken edit. Never add measurements as decisions.
4. Review the versioned conversation cases; run the separate opt-in live
   evaluation on an approved configured Claude account when available.
5. Deploy the updated document and restart the backend workers. The loader is
   process-cached; a local file edit does **not** update a hosted server. There
   is intentionally no unauthenticated hot-reload endpoint.

New conversations load the deployed validated document and retain its full
snapshot, declared version and SHA-256 hash. Existing conversations retain their
requirements even after restart/deployment. Invalid documents block new intakes,
without erasing required fields or falling back to the legacy rubric. Runtime
prompt text and wire instructions are also pinned with each intake. Bump the
prompt/schema version and plan a compatibility migration if changing the wire
contract in a future release.

## Configuration and deployment status

1. Install requirements, including pinned `PyYAML==6.0.3`. The Docker build now
   copies both runtime Markdown documents to `/app/docs`.
2. `supabase/migrations/20261019182934_quote_intake_state.sql` was applied on
   October 19, 2026 to **TakeShapeApp** (`pxnbfdsyuzweevkvremi`), the project
   configured by the mobile app. The local filename matches the remote migration
   history. It creates a private, RLS-enabled service-role-only table with an
   owner cascade. Remote queries verified the constraints, indexes, RLS and all
   four CRUD privileges: denied for `anon`/`authenticated`, granted for
   `service_role`. The table was empty after migration. Existing scans are authorized
   through the existing upload ownership records. Without Supabase configuration,
   local development uses SQLite under `LIDARAI_STORAGE_DIR`; configured Supabase
   failures never fall back to the local database.
3. Use the existing `LIDARAI_ANTHROPIC_API_KEY`, `LIDARAI_ANTHROPIC_MODEL`,
   `LIDARAI_ANTHROPIC_EFFORT`, timeout and max-token settings. Do not change
   `LIDARAI_AI_PROVIDER` to activate intake. Credentials stay on the backend.
4. Verify the configured model/account supports structured outputs and the chosen
   effort. The installed official SDK is `anthropic==1.0.0`; the new adapter uses
   its `messages.parse(output_format=...)`, with local semantic validation and
   at most one corrective regeneration. Current official documentation confirms
   this SDK contract. The checked-out default remains `claude-sonnet-5` / `low`.
   The supplied account key is configured in the Git-ignored `backend/.env`
   with owner-only file permissions. The live Anthropic model lookup confirmed
   access to `claude-sonnet-5`. This local configuration does not modify Render's
   environment. See the live evaluation record below for conversation results.
5. Deploy the backend before installing the updated iOS app. Existing legacy
   routes keep working during rollout. Build scheme `LidarAI`, bundle
   `com.noahwaldron.takeshape`, deployment target iOS 18.6. No deployment or
   TestFlight publication was performed here.

## Evaluation and regression checks

`tests/fixtures/quote_intake/conversations_v1.json` includes 15 synthetic cases:
complete/incomplete scope, ambiguous intent, multiple fields/corrections,
uncertainty, roof leaks with interior scans, multiple/unsupported trades, early,
leading-zero, spoken, extended and ambiguous ZIPs, moving destination, pause and
prompt injection. `scripts/evaluate_quote_intake.py` validates fixtures by default
without network access. For an approved account, run from `backend`:

```sh
PYTHONPATH=. ../.venv/bin/python scripts/evaluate_quote_intake.py \
  --live --output /tmp/quote-intake-live-v1.json
```

The live runner never imports submission/email APIs or accesses scan data.
Its report labels real-model use and records model/effort, rubric hash,
clarification turns, repeated field questions, latency, reply length, structured
answers and provider briefs. Automated expected outcomes are necessary but do
not replace reviewing unnecessary/repeated questions, fabricated facts,
unsupported visual claims, quote-brief completeness and abandonment risk.
Reports also include prompt version/hash and validation attempts. Use repeated
`--case CASE_ID` arguments to run selected fixtures during investigation.
Reply length/turn count are friction indicators, not measured abandonment rates.
Required release targets: zero geometry questions, fabricated facts, unsupported
visual claims and duplicate submissions. The runner intentionally has no
submission calls; duplicate protection is tested separately at the API boundary.

The deterministic backend tests mock Claude and delivery and cover persistence,
CAS fencing, ownership, malformed-output bounded retry, replay ordering,
immutable pending submission/lost success, upload waits, pause, rubric pinning,
provenance, conditional applicability and input allowlisting. UI fixtures never
call Claude or send leads. Mobile has its own owner/scan/conversation/visit
binding, persisted turn IDs, transcript/draft recovery and immutable retry body.
Existing bubble/composer/header drag, keyboard scrolling, model fullscreen,
retry, ceilings, returned quotes and acceptance UI remain in place.

Sources checked: [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs),
[Anthropic model capabilities](https://platform.claude.com/docs/en/models/overview),
[Supabase authenticated user lookup](https://supabase.com/docs/reference/python/auth-getuser),
[Supabase changelog](https://supabase.com/changelog.md). The existing backend uses
server-side Auth `/auth/v1/user` verification and PostgREST, not unverified JWT
metadata. The database migration creates only the new intake table; it does not
modify existing customer records or send provider messages. Hosted credentials
were not changed.

## Local verification record — October 19, 2026

- Full backend suite rerun before the GitHub handoff: **1,366 passed, 2 skipped**.
  After the final brief-rendering change, the focused intake/direct-submission
  suites passed **75 tests** (60 intake, 15 direct). Existing FastAPI lifespan
  deprecation warnings remain. All Claude, Supabase and operations traffic in
  tests is mocked; no real lead is created.
- The official installed Anthropic SDK was exercised through a mocked HTTP
  transport, confirming schema transformation/parsing retains the new fields,
  uses the configured effort and sends text-only content.
- Local PGlite migration execution passed service-role grants, client-role
  denial, RLS, owner deletion cascade and compare-and-swap fencing. Reproduce
  with `PGLITE_MODULE=/path/to/@electric-sql/pglite node
  backend/tests/sql/quote_intake_state.cjs` (tested with PGlite 0.3.14).
- Remote migration verification passed on TakeShapeApp. The Supabase security
  advisor added only the informational
  [RLS enabled without policies](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy)
  notice for `quote_intakes`, which is intentional for backend-only access.
  No new warning or error was introduced. Existing project findings include
  [five unrelated tables without RLS](https://supabase.com/docs/guides/database/database-linter?lint=0013_rls_disabled_in_public)
  and existing function/Auth warnings; those were not modified by this migration.
- The iOS simulator build succeeded. Focused unit checks passed for binding,
  persisted retries, wire allowlisting, actual delayed-response A → B → A
  recovery, legacy scan-binding/retry helpers, ceiling detection and scene loads.
- Simulator UI checks passed spoken-ZIP submission, upload/keyboard layout,
  draft and pending-submission relaunch, queued-upload recovery, model retry,
  fullscreen draft preservation, returned PDFs, onboarding return, long-history
  scrolling, model-divider resizing, measurements and callback approval.
- A parallel simulator run encountered launch/resource failures on a nearly
  full disk. The serial regression rerun passed. A separate test launch crashed
  in the existing USDKit/SceneKit synthetic model exporter inside
  `UITestSupport.writeModel`, before intake UI execution. The isolated pause-flow rerun passed, including
  the surfaces question and stopping while upload is pending. No model-rendering code was changed to mask it.
- The first live run exposed omitted common fields, redundant questions and
  invalid answer representations that the original mocked tests did not expose.
  The wire instructions, schema guidance and bounded repair path were corrected;
  regression tests now cover candidate-preserving repair and omitted common
  fields. Account access to the configured model is verified. See the live
  evaluation record for the final synthetic conversation results.

The dashboard also recovers immutable requests queued by the previous scripted
panel through its existing direct-submission helper. Unsent pre-agent draft text
is kept in the composer; it is not fabricated into a legacy chat turn. New
intakes and submitted receipts keep separate identities.

## Live evaluation record — October 19, 2026

The final [saved report](evaluations/quote-intake-2026-10-19.json) records
**15 of 15 scenarios passing**, with **18 homeowner turns**, **21 Claude calls**,
and **zero repeated field questions**. It used `claude-sonnet-5`, effort `low`,
and prompt `quote-intake-v1.1`. Turn latency, including any question review, was
8.0 seconds median and 10.2 seconds maximum in this run. No provider request,
email, or submission API was called.

Review covered replies, cited answers, and provider briefs. A couple of
freeform model paraphrases in the recorded state could overstate assessment
intent or imply scan coverage. The final provider renderer therefore uses the
exact cited homeowner wording for freeform scope. The recorded validated
responses were replayed through that renderer without more model calls; their
structured answers were checked for equality. That renderer also has a focused
regression test. This synthetic sample does not establish universal semantic
accuracy or measure abandonment; provider review still uses the attached
evidence and the original request.

At the validation checkpoint before the GitHub handoff, the migration was live
and the backend/iOS code had not been deployed. Pushing backend `main` can trigger
Render's configured automatic deployment; verify its deployed SHA separately.
Render's environment was not changed. The supplied key remains only in the
ignored local backend configuration and was not included in source files or
this report. [Deep-testing prompt for the next thread](QUOTE_INTAKE_TESTING_HANDOFF.md).
