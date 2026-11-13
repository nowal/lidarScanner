# Active-thread recovery and latency — November 13, 2026

## Integrated source

The working branch starts at backend main `dbb2e680e7e56864c57e3f3b4fecfd504db45e8c`. Quintin's November 3–5 changes include repeated-question safeguards, processing/retry metadata, conditional resale questions, provider briefs, branded acceptance emails and provider-supplied materials. The matching mobile base is `8faa3d3` (durable retry recovery and merge of the saved-thread release). Both repositories changed after the November 2 release.

Render service `srv-d8oqeksm0tmc73d1vdl0` was verified live at deploy `dep-dbmctn8473hc7382neeg`, commit `dbb2e68`, finished November 5 at 18:46:33 UTC. The running plan is Pro, one instance. These fixes have not been deployed. Quintin's installed iOS build is unknown; build 19 remains the last locally documented Apple upload, not a newly verified tester build.

## Established recovery failure

Four retained `/active` failures on November 6, 8, 10 and 12 returned 403 after successful Supabase authentication. Their ownership checks found no scan index and queried upload ownership. Read-only database timestamps show that each scan's first asset arrived **1.9, 3.7, 4.2 and 6.3 seconds later**, respectively. This is a scan-upload/restore race, not evidence of an expired-token failure. It predates Quintin's latest changes. The reported long-idle case cannot be tied to an exact build/request without phone logs.

The server now checks the small authoritative `scan_upload_heads.owner_id` before hydrating an entire index. Missing first-upload ownership returns `409` with code `scan_ownership_pending`; a foreign or conflicting owner remains denied with 403, failed ownership reads remain 503, and missing/invalid identity remains 401. This does not create an ownership record or allow a caller to claim an arbitrary scan. Submission still separately verifies scan readiness and revision.

Mobile recovery retries only the explicit pending-ownership condition, after 1, 2 and 4 seconds, and subsequently responds to upload indexing/completion. It also recognizes the old deployed error text. It does not retry general 401/403/503 responses. Cancellation is silent; real errors retain category/status diagnostics. Binding checks fence account/home/thread changes. Active restoration replaces the previous serial GET + POST sequence with one POST, and Retry restores the active pointer even when a cached transcript exists. A persisted unfinished turn continues after recovery with the same turn ID. Saved text, photos, immutable turn/submission payloads and legacy migration IDs remain intact.

Only a locally completed ready turn records permission to submit that exact revision automatically after upload/relaunch. Old/cross-device ready history without that marker waits for the explicit Send saved request action. Existing uncertain submission payloads still recover their original receipt.

## Latency change and limits

The model, low effort, 8,192-token ceiling, rubric decisions and full transcript are retained. Output limits are ceilings, not observed output length. No scan/image bytes or home-index payload is sent to the intake model. The old routine re-sent roughly 36,000–39,000 input tokens per model call, mostly static instructions/rubrics, and regenerated the entire Result during a second sequential question review.

The transport now places unchanged rubric/catalog data before dynamic conversation data with an explicit five-minute cache boundary. JSON serializer spaces are omitted without changing values. A review can return `unchanged=true, revisedResponse=null` (21 output tokens in the sample) after verifying the question; necessary corrections still return the complete Result and pass the original provenance/readiness checks. If application validation inserted or changed a question, the full review is required. Invalid results still get bounded repairs. An explicit current-turn ID prevents older transcript evidence being used as current intent.

An initial paired synthetic benchmark against `dbb2e68`, two repetitions of three identical states, alternated version order. Each version made 8 model calls over 6 turns:

| Scenario | Before, seconds | After, seconds |
| --- | --- | --- |
| Short clarification | 8.565 / 8.731 | 7.523 / 7.055 |
| Complete scope | 6.646 / 9.286 | 6.244 / 7.000 |
| 51-message saved thread | 7.211 / 7.203 | 8.053 / 7.089 |

Mean over these six turns: **7.940 → 7.161 seconds** (9.8%). This is a small generation-only sample, not user-visible end-to-end latency or a population percentile. Long-thread timing did not improve consistently. The first optimized clarification had zero cache-read tokens (cold prefix); subsequent normal calls reused 34,809 static tokens, with a separate cached review prefix. Across the optimized run: 7,682 uncached input tokens, 70,127 cache-write tokens, 209,363 cache-read tokens and 3,547 output tokens. Baseline: 296,717 input and 4,220 output tokens. No quote or email was sent.

An earlier candidate caused extra long-thread intent repairs; explicit current-turn identification corrected both paired reruns. The production database has 23 intakes with 0–18 transcript messages, mean 8.7, and pinned system instructions from 14,647–30,777 characters. Existing threads keep their pinned rules. The 51-message benchmark intentionally stresses more history than that observed maximum. The latest prompt prose grew about 5% since November 2 (22,798 → 23,936 characters); the second review call and large prompt already existed before that release.

A second paired run on the final restricted-review code overlapped an Xcode rebuild and had much larger variation: **11.256 seconds baseline versus 14.964 seconds optimized**, again six turns per version. Both passed all scenario checks. Baseline made 9 model calls, optimized 8; optimized calls used 7,671 uncached input, 35,318 cache-write, 244,172 cache-read and 3,593 output tokens. Concurrent machine load was observed, but its exact contribution versus remote model variation was not isolated. Thus the initial 9.8% reduction is provisional, not a repeatable speedup claim. Reduced duplicate fetches, cache hits and shorter unchanged reviews are verified; stable end-to-end improvement remains unmeasured.

The retained Render logs contained no per-model token timings and the `/turn` metric query returned no series. Thus there is no defensible production p50/p95, queue-time attribution or physical-phone idle/resume timing from this investigation. A direct local DB-network benchmark was unavailable because this checkout has no Supabase server connection configured. Read-only production diagnosis used the connected Supabase tools. Auth and model transports still construct per-call clients; their separate handshake cost was not isolated, so it is not included as measured savings.

New privacy-safe `Server-Timing` and request logs report auth, ownership, store reads/writes, generation, model and total server time. Ownership includes auth; generation includes model, so these are nested spans and must not be added together. Phone logs report session, HTTP response bytes/time and send completion time. They exclude message bodies, credentials and owner IDs. Physical-phone rendering, end-to-end networking and long-idle refresh remain release validation items.

## Validation

Full deterministic backend run: 1,488 passed, 2 skipped; 3 existing email-copy assertions fail identically on untouched `dbb2e68`. Those assert retired headings/internal scope labels/request-ID subject text after Quintin's branding updates (`test_quintin_oct6_rubrics`, `test_quote_decisions_sep17`, `test_scan_scope`). They are not masked or rewritten here. Targeted new tests cover pending versus foreign ownership, failed DB reads, unchanged/corrected/full review output, exact payload preservation and privacy-safe timing headers. Existing thread/CAS/receipt/approval tests pass.

Live adaptive integration: all 5 scenarios passed in both runs, each over 9 homeowner turns (12 model calls initially, 13 on the final restricted-review code), covering complete scope, consultation then ZIP, window-specialty clarification, landscaping routing and pausing. Every citation passes application validation, and the benchmark retains supported answers. Both benchmark runs and integration checks are recorded in [the evaluation report](evaluations/restore-latency-2026-11-13.json).

The older 15-scenario model fixture returned 7 passes and 8 failures. Seven mismatches also reproduce on main because its zero-question readiness expectations predate expanded rubric decisions. The eighth is an intermittent unnecessary `desired_help` question for an unsupported plumbing diagnosis. Three additional runs on both main and this branch each produced one pass and two failures. This existing extraction weakness is recorded rather than hidden by changing expected results or relaxing readiness. A full review is now mandatory whenever application validation supplies a missing question; the shortened review is limited to the model's own unchanged proposed questions. These evaluations do not establish population-level quality equivalence.

Mobile validation includes 24 named intake unit tests (26 parameterized executions) and six fixture UI flows. They cover pending ownership, cancellation, offline recovery, account changes, exact queued revisions, immutable submissions, explicit saved-request sending, paused requests and thread reopening. No test contacts a production provider.

The cache boundary and schema behavior follow the provider's [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) and [structured output](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) documentation. Reproduce the paired synthetic check from `backend` with `PYTHONPATH=. ../.venv/bin/python scripts/benchmark_quote_intake.py --live --compare-ref dbb2e68 --repeats 2 --output /tmp/quote-intake-benchmark.json`. This explicitly makes billable synthetic model calls; no API/submission module is imported.

Production operations were read-only. No schema migration, deployment, customer request mutation, quote submission or email test was performed. Supabase health advisories returned no findings; existing unrelated security advisories were left unchanged. The durable ownership table remains service-only with RLS.
