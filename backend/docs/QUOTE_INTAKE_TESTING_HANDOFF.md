# Quote-intake deep testing handoff

This is a ready-to-paste prompt for a new Codex thread. Use the current `main`
branches of both repositories. The checkout paths on Quintin's machine may
differ from the paths in the original implementation handoff.

```text
Test TakeShape's new quote-intake agent and its iOS integration in depth. Run
the checks, investigate failures, fix reproducible product defects, and add
meaningful regression coverage. Do not stop after proposing a test plan.

Repositories:
- Backend: https://github.com/nowal/lidarScanner (backend/ directory)
- iOS: https://github.com/nowal/takeshape-mobile (private repository)

Find both local checkouts, inspect Git status and applicable AGENTS.md files,
and record the exact starting commit SHAs. Preserve existing local changes.
Work on a separate codex/quote-intake-deep-testing branch or suitable isolated
checkout. Do not assume the obsolete lidarTakeShape/LidarAI project is active.
Adapt the original handoff's absolute paths to these checkouts.

Read completely:
- Mobile docs/quote-intake-agent-handoff.md
- Backend backend/docs/QUOTE_INTAKE_SYSTEM_PROMPT.md
- Backend backend/docs/QUOTE_INTAKE_RUBRICS.md
- Backend backend/docs/QUOTE_INTAKE_IMPLEMENTATION.md
- Backend backend/docs/evaluations/quote-intake-2026-10-19.json
Also inspect the existing direct submission, scan upload/snapshot, provider
quote/PDF/approval, and legacy agent contracts before changing integration code.

Current baseline:
- A separate text-only Claude intake owns its state and validated, versioned
  rubrics. The legacy agent/global provider configuration and current dashboard
  design are meant to stay intact.
- The latest full backend suite passed 1,366 tests with 2 skipped. Focused
  intake/direct-submission suites passed 75 tests.
- The saved live synthetic evaluation passed 15/15 scenarios: 18 homeowner
  turns, 21 model calls, no repeated field questions, about 8.0 seconds median
  and 10.2 seconds maximum turn latency. It never submits requests or sends
  mail. A synthetic sample is not evidence of universal semantic accuracy.
- Simulator builds, focused unit tests, and the documented UI regressions
  passed. Read the release/testing record for any later complete unit run.
  A complete mobile UI-suite run and physical-device end-to-end intake test
  are not established by those focused results.
- Earlier parallel simulator attempts hit disk/resource problems; one isolated
  launch crashed in the existing USDKit/SceneKit synthetic model exporter
  before intake execution. Serial/isolation reruns passed. Keep infrastructure
  failures distinct from actual product failures, with evidence.
- Migration 20261019182934_quote_intake_state.sql is already applied to the
  TakeShapeApp Supabase project. It creates a service-role-only RLS table.
  Do not reapply it or modify production data as part of testing.
- GitHub main may auto-deploy the backend through Render. Verify the deployed
  SHA and route availability separately; a push is not deployment evidence.
  No new iOS/TestFlight release was produced by the intake implementation.

Test environment and boundaries:
Use local mocks and an isolated test backend/database first. Use only synthetic
homeowner content for live Claude checks. Read LIDARAI_ANTHROPIC_API_KEY from
secure local environment configuration; it is deliberately absent from Git.
Never print, commit, put in fixtures, or send credentials. Keep outbound
operations/provider delivery captured by a test sink. Do not send real leads,
provider/customer messages, approvals, or contacts. Do not deploy, merge, push
to main, or publish TestFlight as part of this testing task. If a needed test
account, device, or environment is unavailable, keep running independent
checks and clearly report the remaining limitation.

Work through these areas:

1. Reproduce the automated baseline and inspect test quality.
   Create the Python environment from backend requirements as needed. From
   the backend repository root, run the full backend/tests suite with
   PYTHONPATH=backend, validate the rubric module, and validate the evaluation
   fixtures. Run the migration checks in backend/tests/sql/quote_intake_state.cjs
   with PGlite or an isolated Postgres setup. Test real PostgREST/CAS/RLS behavior
   in an isolated Supabase test environment if available; do not confuse the
   SQLite fallback or mocked HTTP with remote persistence coverage.
   For opt-in model evaluation, run from backend/ with the configured key:
   PYTHONPATH=. ../.venv/bin/python scripts/evaluate_quote_intake.py --live
   --output /tmp/quote-intake-deep-live.json
   Adjust the interpreter path to the environment you created. --case CASE_ID
   can select individual fixtures. Do not weaken an assertion just to pass.

2. Expand semantic testing beyond the existing 15 scenarios.
   Cover all 13 catalog services, relevant subtypes and conditional decisions,
   unsupported trades, multiple services, adding/removing/rerouting work,
   several successive corrections, contradictory answers, uncertain/no-
   preference/recommendation responses, and ambiguity that persists after a
   clarifier. Add held-out phrasing, typos, short replies and longer histories.
   Test early/spoken/leading-zero/ZIP+4/ambiguous ZIPs, a moving destination
   versus the home ZIP, pause/cancel/browse/resume and changes while upload waits.
   Test prompt injection mixed with a legitimate request and quoted instructions.
   Distinguish an assessment or recommendation request from permission to repair.
   Inspect freeform stored interpretations as well as the exact-source provider
   brief: source-span validity alone does not prove semantic correctness.
   Check that repeated corrections preserve the complete final intended scope
   without silently reverting earlier corrections or expanding scan coverage.
   Never ask dimensions, geometry, visible object counts or technical inspection
   questions; never claim scan access, fabricated facts, pricing or submission.
   Measure unnecessary/repeated questions, turns, latency, failures/retries and
   brief completeness. Repeat a bounded set of difficult cases to expose model
   variability; report sample sizes and call counts honestly.

3. Exercise failure, concurrency, identity and recovery paths.
   Test two workers, concurrent duplicate turns/submissions, stale revisions,
   replay of old turn IDs, changed bodies with reused IDs, expired leases,
   worker death between persistence/model generation/delivery/receipt storage,
   and a lost success response. Test 429/5xx/timeouts, malformed/refused/truncated
   model output and failures on the second generation attempt. Verify the total
   two-call bound and honest recoverable errors. Assert no accepted answers or
   pending immutable request are lost and exactly one submission is delivered.
   Test wrong owner, missing/expired auth, cross-home access and concurrent
   owner/session changes. Confirm there is no local fallback after a configured
   Supabase failure. Verify the new direct payload remains compatible with old
   fingerprints and preserves immutable scan revisions and explicit scope.

4. Test the iOS integration and dashboard regressions.
   Project LidarAI.xcodeproj, scheme LidarAI, bundle com.noahwaldron.takeshape,
   deployment target iOS 18.6. Select an installed simulator and run serially
   with -parallel-testing-enabled NO when resources are limited. Run the full
   unit suite, then the dashboard/onboarding UI suite and additional failure
   scenarios. Cover A -> B -> A scan navigation, owner changes, late callbacks,
   relaunch/background/foreground, interrupted turns, unsent edits, upload
   completion racing with pause/edit, and uncertain pending submission recovery.
   Verify keyboard/long-history scrolling, model divider drag, touch hit areas,
   collapse/expand, fullscreen and measurements, visible ceilings, model-load
   retry, quote polling, returned PDFs, signing/callback, and contact-release
   boundaries. Try small/large screens, dark mode and larger text. Preserve the
   current dashboard UI instead of redesigning it to make tests easier.
   Physical LiDAR capture, real network loss, memory pressure and the complete
   scan -> upload -> intake -> test delivery -> quote -> approval path need a
   supported real iPhone and isolated test services. Do not mark them passed if
   you only ran fixture-based simulator tests.

5. Verify isolation and deployment readiness.
   Prove the new model request contains only the permitted intake text,
   structured answers, catalog/rubric and minimal workflow facts. No scan
   images, geometry, media URLs, legacy context or credentials may reach it.
   Test valid/invalid rubric edits, duplicate keys/IDs, conditions and cycles,
   restart behavior and pinning of existing conversations to prompt/rubric
   snapshots. Check Docker includes the runtime documents. Verify public and
   authenticated clients cannot access the intake table directly. Keep existing
   Supabase advisories separate from anything this change introduces.

Deliver a concise testing report with exact repo SHAs, environments/devices,
commands, observed results and evidence, bugs ordered by severity with concrete
reproductions, fixes and focused regression tests, and remaining untested paths.
Give a clear deployment recommendation with any blockers. Distinguish mocked,
live-model, database-integration, simulator and real-device evidence. Never claim
production delivery or a full mobile pass from a smaller set of checks.
```
