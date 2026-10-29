# Quote submission media

## October 29: sharpest photo per three seconds — GitHub testing branch, unreleased

For Quintin's pull/test handoff, use `codex/quintin-intake-integration` in both
`nowal/lidarScanner` and `nowal/takeshape-mobile`. Fetch origin and check out that
branch in each repository; Python code is still under `backend/`. Use the backend
branch with mobile context format 3 before testing new scan uploads. This branch
is for testing, with no production deployment or TestFlight release authorized.

Focused backend checks (from the repository root):

```sh
PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_quote_media.py backend/tests/test_home_index.py backend/tests/test_staged_scan_uploads.py backend/tests/test_durable_scan_uploads.py backend/tests/test_direct_quote_requests.py -q
```

For the isolated synthetic viewer, run
`PYTHONPATH=backend .venv/bin/python backend/tests/browser/serve_quote_media_preview.py`
and open the freshly printed URL. The fixture disables production storage/email
integrations. The two browser scripts under `backend/tests/browser/` accept
`PLAYWRIGHT_MODULE` pointing to an installed Playwright package. The app's current
hosted backend has not been updated by this source handoff; use an isolated backend
running this branch for end-to-end upload tests. Existing sparse snapshots cannot
supply additional frames without a new upload from the original capture archive.

This supersedes the October 28 every-20th-frame sampling policy. Mobile context
format 3 writes `providerSelectionVersion=2`, `providerFrameWindowSeconds=3`,
`providerSamplingPolicy=sharpest-per-time-window` and the selected IDs. The old
`providerFrameStride` field is removed. The viewer, lazy image delivery and
revision-pinned media snapshot format (still version 2) are unchanged.

Within each capture area, windows are anchored at the earliest saved ARKit
`timestamp`: [start, start+3), [start+3, start+6), etc. Each nonempty window,
including the partial tail, chooses the highest-sharpness readable image. A
missing/corrupt winner falls back to the next candidate in that window; empty
or wholly unreadable windows produce no photo. Equal scores prefer the earlier
timestamp and then manifest order. This is approximately one photo per three
seconds, not a minimum three-second gap between adjacent selected frames.

The phone already records `sharpnessScore`, a Laplacian-variance blur proxy.
When every candidate in a window has a valid finite nonnegative score, it ranks
those scores and decodes only candidates needed to find a readable winner.
If any score is missing/invalid, all candidates in that window are measured
using the same <=320-pixel grayscale JPEG metric, one at a time; capture and
JPEG score scales are never mixed. Sharpness is a heuristic affected by scene
texture/noise, not a guarantee of perfect focus. There is no new AI image analysis.

If an area has no usable capture timestamps, `capturedAt` supplies the clock
(ISO-8601 or Codable reference-date seconds). Untimed frames are used only as a
single best-photo fallback when no timed photo is usable. No frame rate or
missing timestamps are fabricated. UUID validation and duplicate removal remain.

The backend accepts both sampling versions and preserves the source version
through semantic-room mapping into the snapshot's `providerSelectionVersions`
(and singular `providerSelectionVersion` when all selected frames share it).
The legacy AI still receives its unchanged up-to-four selected images. All
resource limits, pose/intrinsics handling, scope/revision authorization and
supplemental-photo access from the October 28 work remain in place.

Format-1/2 caches are invalidated for new exports and explicit rebuilds; resumes
keep already uploaded immutable objects and revisions. Existing links are not
resampled. Backend-before-mobile rollout and explicit release authorization
still apply, including accepting selection version 2 before distributing mobile
format 3. Nothing was deployed or uploaded to TestFlight.

Validation on October 29: **33 focused iOS tests passed** across exporter and
upload coordinator suites, including real sharp/blurred JPEG comparison,
three-second boundaries, irregular timestamps, ties, older capture dates,
missing/corrupt images, multiple areas, pose/intrinsics preservation, cache
upgrades, retries and legacy selection. **94 focused backend tests passed** across
quote media, home indexing, staged/durable uploads and direct requests. Current
Swift sources were compiled. Browser UI was unchanged; October 28's browser
and homeowner UI results below are historical, not new runs today.
Logs: `/tmp/takeshape-three-second-ios.log`, `/tmp/takeshape-three-second-backend.log`.
iOS result: `/tmp/takeshape-quote-intake-build/Logs/Test/Test-LidarAI-2026.10.29_11-29-45--0400.xcresult`.
Physical-device capture verification remains a release check.


## October 28: denser provider scan photos — local, unreleased

New mobile context format 2 carries the union of two independent selections:
`providerSelectionVersion=1` / `providerSelectedFrameIds` / `providerFrameStride=20`,
and the unchanged `aiSelectionVersion=1` / `aiSelectedFrameIds` (at most four).
The stride uses the complete saved capture sequence in each area, before image
availability checks: finite ascending capture `timestamp`, manifest order for
ties, then missing/nonfinite timestamps in manifest order. Valid, unique UUID
records count toward ordinals 20, 40, 60, etc. A nonempty area shorter than 20
uses its first readable image. Missing/corrupt sampled images are skipped,
without shifting the stride or substituting adjacent frames. Invalid/duplicate
IDs are ignored. Pose validity does not discard a readable provider photo.
The phone still resizes copies to 768 pixels, retaining native image rotation,
intrinsics and their original reference resolution. Raw capture files stay intact.

`HomeIndex.Room.providerFrames` is separate from legacy `frameIds`. Both use the
same camera-position / polygon / storey mapping; an unposed provider image falls
back to its capture area. The provider descriptor retains `captureArea` as well
as its mapped semantic room, so an upstairs photo can be read from a different
area's archive without moving its camera or confusing model scope. These fields
survive durable context ingestion and model publication. The legacy selector and
vision budget remain unchanged; Quintin's intake remains text-only.

New provider-aware snapshots are version 2. They pin photo IDs, camera metadata,
scoped rooms, context path, model paths, owner and scan revision at submission.
Submission/email preparation writes only metadata for these photos. `/status`
returns no storage paths and does not decode or download their images. A photo
request prepares only that photo, then redirects to the existing private,
short-lived signed URL. A deterministic derivative name plus durable ready
receipt makes interrupted uploads retryable without changing the snapshot.
One photo worker per process bounds decoding/storage work; other requests wait
up to five seconds before a retryable 503. Completed derivatives survive restart.
First access to each new photo downloads the pinned ZIP to temporary disk, so
first-photo latency and repeated ZIP transfer for different photos remain tradeoffs.
No persistent local cache, public bucket or new database migration is required.

The 32 MiB ZIP download, 128 MiB total unpacked size, 5,000 ZIP-member and
16-megapixel image limits remain. Both durable and older staged context ingestion
now validate before extraction; traversal, symlinks, encrypted/duplicate members
and JSON members over 4 MiB are rejected. The phone fails explicitly if its
context exceeds the shared size/member contract; it never silently increases the
stride. Legacy full-export ingestion retains its separate existing contract.
Older photo preparation streams derivatives one at a time. Older snapshot pose
backfill reads metadata only and preserves existing poses and supplemental photos.
The old downstream four-photo cap is removed.

The viewer removes the below-model scan gallery. All posed photos get compact,
keyboard/touch-accessible markers. `Show scan photos` retains its checked state
across area changes and measurement mode; measurements temporarily suppress the
markers. Only the selected/hovered/focused marker has a direction arrow, with
projection updates coalesced to animation frames. The in-viewer Photos picker
includes supplemental homeowner photos and unposed older photos, and works
while the model is preparing, failed or absent. Only opening a photo requests
its full image; there are no eager gallery thumbnails. Previous/Next, arrow keys,
Escape, Back to model, focus restoration, measurements and PDF submission remain.

Existing ZIPs containing four images cannot produce missing capture frames.
Existing submitted requests and links keep their original revision and available
photos. On mobile, resume/retry keeps an already uploaded context object. An
explicit rebuild creates a new revision and replaces old-format context caches;
completed scans are never silently upgraded on reopen. An intentional new upload
from the original local capture archive is required for denser photos, followed
by a new request if a provider should receive the newer revision.

### Validation on October 28

- Full backend suite: 1,433 passed, 2 skipped; existing lifespan deprecation
  warnings remain. Includes dense/scoped/revision-pinned snapshots, direct request
  retries, private API access, pose/room/storey mapping, legacy sparse backfill,
  corrupted/oversized images, bounded archives, durable upload and legacy vision
  isolation. External storage/email integrations are mocked.
- Both local browser scripts pass with 240 posed images in two areas plus a
  supplemental photo and an unposed photo. Checks include no eager image requests,
  no gallery, keyboard and pointer marker access, persistent toggle state, scoped
  navigation, pending/failed/absent models, measurements and PDF receipt. The
  60-frame orbit check took about one second in local Chrome. Desktop/mobile
  screenshots were inspected; this is a synthetic stress fixture, not a claim
  about all customer models or browsers.
- iPhone 17 Pro / iOS 26.5 simulator: 41 focused unit tests and 3 dashboard UI
  tests passed (automatic intake submission, upload-wait visibility, returned
  PDFs). Xcode compiled current exporter/coordinator and homeowner view sources.
  Result: `/tmp/takeshape-quote-intake-build/Logs/Test/Test-LidarAI-2026.10.28_14-49-05--0400.xcresult`.
- Logs: `/tmp/takeshape-dense-backend-full-final.log`,
  `/tmp/takeshape-dense-ios-final.log`, `/tmp/takeshape-dense-browser-form-final.log`,
  `/tmp/takeshape-dense-browser-measure.log`.
- No real customer submission, email, production change, Git push or TestFlight
  upload. Physical-device capture/upload remains a release check.

### Backend-before-mobile rollout (requires Noah's release authorization)

1. Review and deploy the backend first, including its viewer assets, new context
   archive module and provider metadata handling. Keep the existing private
   storage configuration and revision transition schema; no migration is needed.
2. In an isolated environment, verify a legacy sparse link and a format-2 capture
   through ingest, completed model publication, scoped submission, on-demand
   private photo loading, and PDF response. Use captured/mock email delivery.
3. Build/test the mobile update on a physical iPhone with a new scan or intentional
   rebuild of a locally retained archive. Confirm frame stride, portrait rotation,
   floors, upload retries/readiness, marker visibility and homeowner intake.
4. Release mobile/TestFlight only after separate authorization. Old saved requests
   remain pinned. A backend rollback before this change would not understand v2
   lazy-photo descriptors; keep backward-compatible backend support once mobile
   format 2 is distributed.

The dated deployment sections below are historical; they do not deploy this change.


## October 15: direct requests and complete acceptance contacts — backend deployed

The hosted backend is deployed from `b8f2e14f3166ff7f410c2781c9cf5e6534507d32`.
Render deployment `dep-db8h8uu0tbcc738db0cg` completed successfully at
2:00:22 PM EDT on October 15. GitHub deployment `7100335333` confirms that SHA.
The submission failure reported from the locally built app was a rollout
mismatch: the previous hosted OpenAPI document lacked the direct POST route,
and an empty request returned HTTP 405. After deployment, `/health` returns
200/ok, OpenAPI exposes the complete direct contract, and the same unauthenticated
empty request correctly returns 401. No synthetic lead or email was created.
The app can retry its saved submission ID/body without rescanning or a new
TestFlight build. Mobile's clearer temporary-unavailability message is committed
on local `main` separately; TestFlight remains unchanged.

The dashboard defaults to Request Quote, with AI Chat (Beta) beside it. Both
modes retain returned quotes and private PDF/approval access. Direct requests use
`POST /api/v1/ai/quote-requests/direct` with `submissionId` (UUID), `homeId`,
`revision`, `description`, string `zip`, optional `roomKeys`, and optional
`homeContext`. Description is required (up to 10,000 characters), preserved
verbatim in the synopsis, and never synthesized from conversation history. ZIP
accepts five digits or ZIP+4, including leading zeroes. No contact, service,
materials, opening turn, or conversation completion is required. The original
AI endpoint retains its slot and identity gates.

The direct route checks the current guest/signed-in homeowner and scan owner,
requires a matching completed revision and immutable context/model snapshot,
and takes scope only from this request. Omitted room keys mean the submitted
whole scan. Explicit keys must exist. Deterministic owner/submission IDs and
create-only durable inserts prevent a retry from overwriting a receipt; changed
payloads with a reused ID are rejected. Retry the *identical* body after an
uncertain result. The mobile app saves that body before sending, restores it
after relaunch, and retries pending submissions on resume. Drafts and all tracked
request IDs are stored per scan; chat and direct requests can coexist.

`POST /ai/quote-requests/{request}/quotes/{quote}/approve` now accepts JSON:

```json
{
  "contact": {
    "fullName": "Test Homeowner",
    "phone": "6155550199",
    "email": "test@example.com",
    "address": "123 Main Street, Nashville, TN 37203, Unit 4B"
  },
  "signedPDF": "optional base64 PDF bytes"
}
```

Omit `signedPDF` for a callback. The bounded JSON envelope allows the entire
contact/signature decision to be validated together; decoded PDFs retain the
25 MiB limit and separate private content-addressed storage. A failed upload or
durable save never approves the quote. Completed retries reuse the saved
approval. The original provider PDF is unchanged. Contact prefill is an
owner-authorized, no-store GET at `/ai/quote-requests/{request}/contact`; it does
not collect, approve, or notify. Corrected contact data is saved on the chosen
quote and appears in the authorized operations view only after approval.

Build 16's raw PDF and phone-only JSON contracts remain usable only if the
request already contains complete valid contact details; otherwise they return
HTTP 409 asking for an app update. Legacy `/select` and approval through `/decide`
always return 409 directing clients to the contact approval flow. `/decide`
continues to support declines. Deploy this backend before distributing the new
app; older installations with incomplete contact data must update to accept.

Initial quote-request and acceptance messages retain `settings.ops_email` and explicitly CC
`noah@takeshapehome.com`. A read-only check of stored production email payloads
on October 15 found `quintin@takeshapehome.com` as the existing recipient
(spelled Quintin). The setting is not replaced. Initial emails preserve additional
configured CC recipients and deduplicate Noah even when already configured.
Inbound command trust is unchanged. Accepted contact name, phone, email, full address,
provider/price, decision method, and private signed-copy link (when applicable)
go to operations for forwarding, never automatically to providers. Initial
provider mail and the general scan viewer do not expose acceptance contacts.
Declines retain their original recipient and omit acceptance contact details.

A pending acceptance-delivery receipt is saved with the approval. A startup-owned
worker checks it every 30 seconds, and approval retries also retry failed mail.
Delivered receipts suppress repeat messages. The stable Resend idempotency key
covers an uncertain transport/durable-receipt retry. As with initial emails,
Resend's 24-hour key retention and SMTP's lack of transactional delivery leave a
rare ambiguous-send window that requires operations reconciliation after an
extended outage; local capture is not mislabeled as live delivery.

Read-only schema/storage inspection confirmed JSONB request records with a text
thread ID, backend-only RLS, and a private `metashape-exports` bucket. No new
migration is needed; do not reapply `20261013200344_provider_pdf_responses`.
Validation: the full backend suite passes **1,306 tests, with 2 skipped**.
Direct-route tests cover exact text/ZIP preservation, immutable model/context/photo
references, selected-area geometry and window dimensions, pending uploads,
ownership, mismatched scan context, concurrent retries, and durable insert failure.
Approval tests cover both complete-contact contracts, legacy bypass rejection,
private prefill, signed-copy storage, cancellation/decline privacy, intended
recipients, unchanged inbound trust, and notification recovery (including requests
beyond the database's first result page). Both isolated
browser scripts pass model measurements, camera photos, price/PDF submission,
and responsive/touch behavior. The new direct-submit-to-email tests use a mocked
transport to verify Quintin and Noah, the exact narrative, a working private
viewer/model redirect for the immutable scan, and identical retries followed by
duplicate suppression. They cover both empty and already-configured CC settings.

Email verification uses isolated fixtures and mocked mail; live deployment
checks only read health/schema and test rejection of an empty unauthenticated
request. The deployed code is pushed to GitHub `main` and integrated locally at
`/Users/noahwaldron/Documents/code/takeshape-backend-staged-upload`.
No TestFlight upload has been performed. The previous October 14 deployment
remains documented below as release history.



## October 14 cleanup — deployed

The provider email now keeps only TakeShape text branding, service/ZIP title,
submission date, homeowner name and ZIP, the withheld-address/contact note,
project/synopsis, scan facts, and one private scan-viewer link. Both HTML and
plain text omit the request number, contact values, logo, provider sourcing
fields, thumbnails, downloads, and legacy quote-entry/reply instructions.

The viewer keeps models, measurements, photo navigation, and Send your quote.
It removes the workspace/revision labels, download links, passive navigation
instructions, and available-date field. A finite nonnegative USD total is
required on the form and API; the PDF limit is 25 MiB. Old stored availability
values remain readable internally but are omitted from the homeowner response.

Homeowner approval uses `POST /ai/quote-requests/{request_id}/quotes/{quote_id}/approve`:
JSON `{ "phone": "…" }` approves a callback; `application/pdf` uploads the signed
copy and approves it. Both require the existing homeowner authorization. The
signed copy is stored separately from the provider's original, under the private
scan folder when available. Approval metadata lives in the existing request JSON,
so there is no new migration. Failed durable writes do not report success.
Unchanged retries do not upload another copy or send another notification.

The existing operations email handoff receives the callback number or a signed
PDF link for the selected provider only. It remains an operations-mediated
handoff; provider submissions still collect a business name rather than an
email address or authenticated provider account. The signed-copy capability
expires after 30 days, mints storage URLs for at most five minutes, and stops
working when the quote is no longer selected. The general scan-viewer link
never exposes approval documents or homeowner contact information.

Validation: full backend suite 1,279 passed / 2 skipped; targeted email rendering
checks also passed after the final scan-fact cleanup. Isolated browser fixtures
verify model measurements, photo navigation, required price, PDF receipt, and
mobile layout. No provider emails or production writes were performed. The
private storage bucket was inspected read-only and has no bucket-specific size
or MIME restriction.

Backend [PR #13](https://github.com/nowal/lidarScanner/pull/13) merged as
`1b6339dec3b99c2463370c286e56930a401881b2`. Render deployment
`dep-db7q05qvcj2c73eeq5i0` completed on October 14, 2026. Read-only production
checks confirmed healthy service, the new approval route, and exact deployed
hashes for the viewer JavaScript, CSS, and measurement JavaScript. A synthetic
unauthenticated approval was rejected with HTTP 401 before any write. No test
email was sent. The matching mobile changes are in
[PR #10](https://github.com/nowal/takeshape-mobile/pull/10); its release document
records the separate TestFlight upload result.

The sections below describe the October 13 release and its rollout history.

Quote submission now captures the single published HomeIndex in the durable quote
record. Its immutable context ZIP, selected room keys/names, frame IDs, model
object paths and revision remain fixed when the homeowner scans again. Whole-home
requests use the home model. Selected-room requests use the matching area models;
ambiguous room names produce an explicit missing-media message instead of guessing.
Legacy requests without a snapshot retain their saved download link; they are not
silently attached to a later scan.

The operations worker reads JPEGs from the saved context ZIP, rotates them using
camera gravity, and saves full (up to 768px) and thumbnail (up to 360px) copies in
the same private `metashape-exports/<owner>/<home>/` folder. Existing scan deletion
removes those files. Up to six representative images appear in the HTML email,
with readable area captions. The browser gallery includes up to four views per
area. Missing images do not prevent delivery. The street address remains withheld.

The 30-day media capability is separate from operations quote-entry authorization. It is
bound to one request and snapshot, permits scan access and PDF response submission, and never
contains a service credential. Each asset request checks the capability and home
existence and redirects to a private Storage URL lasting at most five minutes.
A resend renews access for the same snapshot and invalidates the previous media
capability. Storage buckets and policies are unchanged. PDF responses require the migration
listed in the rollout steps below.

## Desktop conversion

USDZ is retained byte-for-byte for download. The desktop viewer uses a self-hosted,
pinned `@google/model-viewer` 4.1.0 and a GLB derivative. USDZ is not the viewer's
rendering format. OpenUSD 25.11 resolves the actual binary USD and nested USDZ
references; the converter carries triangle geometry, world transforms, stage
units/up-axis, normals, indexed UVs, material bindings, diffuse/emissive textures,
opacity and metallic/roughness factors into GLB. Non-unit source normals are normalized without changing their direction.
Embedded PNG/JPEG bytes are copied
without recompression. UV V coordinates are translated to glTF's convention.
The original is never modified. Unsupported shading/geometry or unresolved/external
references fail the derivative rather than quietly dropping parts of the model.

Conversion runs one at a time in a subprocess (20-minute timeout; Linux 3 GiB address
space and 1,100-second CPU limits). Immutable source hash + converter version keys
the cache. A durable ready receipt follows the completed resumable Storage upload.
Repeated requests reuse it. A restart-interrupted preparation can be requeued on
viewer access; a failed conversion cools down for an hour. The page has preparing,
loading, failure and original-download states. No model attachment or conversion
runs on the quote submission response path.

## Delivery configuration

`LIDARAI_OPS_EMAIL` retains the primary operations address. Set
`LIDARAI_OPS_QUOTE_CC=noah@takeshapehome.com` for submission copies. The setting is
only applied to quote-request lead messages, including explicit resends. Acceptance
emails independently add Noah as described above; decline emails and reply
confirmations keep their existing destinations. Resend and SMTP
receive separate validated recipient lists with duplicate addresses removed.
`LIDARAI_OPS_REPLY_TO` and `LIDARAI_OPS_REPLY_SENDERS` retain their existing meanings;
a CC recipient does not gain email-command privileges.

The rendered submission payload is durably saved before transport, so retrying
uses the same content, recipients and Resend idempotency key. Duplicate queued
work checks the delivery stamp. An explicit authenticated resend gets a new
delivery ID. Resend retains idempotency keys for 24 hours; SMTP cannot provide an
atomic provider/database delivery transaction. The usual rare ambiguous-send
window after an extended database outage still requires operator reconciliation.

## Validation

Run `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests` from the repo.
`test_quote_media.py` covers scope/revision, camera orientation, missing photos,
escaping, private access/expiry, recipient handling, unchanged sender trust,
retry payloads, duplicate delivery and conversion recovery/failure.
`test_usdz_to_glb.py` builds three-level nested packages with known geometry,
transforms, scale, UVs, emissive material and texture bytes, and checks exact
conversion results and external/missing-reference rejection.

Production verification must use one clearly labeled email to Noah only. Never
bulk resend historical requests or send development previews to providers.

## Provider camera markers and PDF responses (October 13)

The viewer now places numbered camera buttons at the selected photos' recorded
ARKit camera positions. The arrows use camera forward (-Z); positions remain in
Y-up metres, the same coordinates retained by area/home exports and the GLB.
Only photos belonging to the selected area are shown (the whole-home view shows
all). Closing the photo dialog, including Escape, leaves the model orbit intact.
The dialog supports previous/next and keyboard arrows. A toggle hides markers.
These are the existing selected context photos (up to four per area), not every
frame from the scan. Markers are annotations drawn above geometry, not occluded
3D camera meshes. Older snapshots backfill poses from their original immutable
context archive; absent/invalid poses retain the gallery without guessed pins.

The same private emailed capability now permits **adding a PDF response to that
request**. It still cannot read other providers' responses, operate the ops API,
change decisions, or reveal the homeowner's street address. The provider enters
a business name (self-reported, not an authenticated provider identity), optionally
a USD total and earliest available date, and uploads a PDF up to 10 MiB.
Availability is stored as a calendar date (`YYYY-MM-DD`) and appears beneath
the provider name and price on the homeowner quote card. It indicates the
provider’s earliest start, not a confirmed booking. Omitting the date preserves
the existing quote layout. Invalid dates are rejected before the file is uploaded;
changing the date counts as changing the submission for retry protection.
This optional value is part of the existing quote JSON and needs no additional
schema migration. File header/trailer and byte limits
are checked, but this is not antivirus scanning. Closed/expired links cannot
add responses. Same-origin intent headers and the existing address rate limiter
protect submission. No email is sent by this upload endpoint.

Each submission has a stable UUID and a content fingerprint. Retrying unchanged
input returns its existing receipt; reusing a UUID with changed content returns
409. Storage objects are immutable and live under the original scan's private
owner/home prefix, so normal home deletion removes them. A separate
`flow_provider_responses` row holds the document reference. Parent-request saves
cannot overwrite concurrent responses. The backend overlays these rows when
reading requests and preserves saved homeowner decisions. Operations deletion
records a tombstone on the parent to keep a removed response hidden. Different
submissions are separate quotes, even if their entered business names match.

Homeowner request reads authorize as before and generate fresh five-minute
Storage URLs, without exposing storage-path fields. The mobile dashboard polls
every 30 seconds while active, immediately checks on resume/request changes,
and renders all responses together with provider names and PDF buttons. Opening
a PDF refreshes its URL and uses a full-screen PDFKit reader with Done/retry.
Polling neither inserts a synthetic chat message nor changes scroll position.

### Rollout and manual check

1. Apply `20261013180250_provider_pdf_responses.sql` **before** deploying the
   backend. It adds a service-role-only table with RLS and parent deletion cascade;
   no existing Storage policy/bucket change is needed. Reads fail explicitly if
   this table is unavailable rather than presenting an empty response list.
2. Deploy this backend and install the updated iOS build. TestFlight build 14 includes the homeowner UI; the backend deployment and
   response-table migration are separate requirements. A TestFlight upload does
   not publish this provider web viewer.
3. Use a current quote-request viewer link. For existing emails the saved scan
   remains fixed; camera metadata can be recovered without resending the email.
4. Check markers against recognizable features in the real scan, open a camera,
   navigate photos, close it, and switch areas. Model/photo fidelity still needs
   a real-device scan check; automated browser checks used a synthetic room.
5. Submit a clearly labeled PDF with one provider name and another with a second
   name. Leave the homeowner dashboard open (up to 30 seconds), or background and
   return. Open each PDF, dismiss, and open the other. Reload the web page and app
   to check persistence. Avoid approving test quotes: existing approval actions
   notify operations and release the address.

### Validation for this change

- 63 focused backend tests pass (responses, media, documents, decisions, request
  guards, and USDZ conversion). The new response tests exercise PDF-only quotes,
  simultaneous providers/stale parent saves, duplicate retry, failure responses,
  authorization, validation, closed requests, decision retention, and deletion.
- Full backend run: 1,150 passed, 2 skipped, 8 failed. All eight are existing
  date-sensitive ranking/email-fixture failures reproduced on unchanged
  `08f21e2`; they are unrelated to these changes.
- Migration executed in local embedded Postgres (PGlite): RLS/grants, duplicate
  primary-key rejection, and cascade deletion verified. No production migration
  has been applied by this development run.
- Desktop/mobile Chromium checks pass for model load, pins and direction arrows,
  photo navigation, returning with an unchanged orbit, area filtering, camera
  toggle, PDF upload through the API, and absence of horizontal overflow.
- iOS simulator build, two-provider full-screen PDF UI test, and both keyboard/
  dashboard-drag regression UI tests pass. Live email,
  production Storage, and TestFlight end-to-end validation remain rollout checks.

Availability follow-up validation: 42 focused response/document/decision tests
pass, including date persistence, invalid calendar dates, changed-date retries,
and compatibility with existing quotes. Browser verification submits a dated PDF
and confirms the same calendar date in the homeowner API. Both iOS calendar-date
unit tests and the updated dashboard/PDF UI test pass.


## Provider measurements and integrated rollout (October 13)

The provider viewer's Measure button enables two-point surface measurements in
feet and inches, rounded to the nearest inch. It uses model-space metres from the
GLB, including the converter's USD stage-unit scaling. Providers can keep multiple
measurements, drag either endpoint, select/delete one or clear all. Orbit/pan/zoom
still work between picks; blank-space clicks and navigation drags do not add points.
Camera markers are hidden while measuring and return on Done. Photo dialogs and
camera toggles preserve the measurements. Each area keeps its own measurements
for the lifetime of the page; refreshing clears them. There is no server write,
measurement export, geometry modification, or new native dependency.

Picking uses the pinned model-viewer 4.1.0 surface raycast and its visible mesh
materials. The browser does not use the native SceneKit ceiling-clipping shader.
Source/API reference: [model-viewer annotations](https://github.com/google/model-viewer/blob/v4.1.0/packages/model-viewer/src/features/annotation.ts).

Integrated with backend main `fb2195c` (including PR #10's provider handoff and
PR #11's assistant scan-update replies). The combined backend passes **1,278 tests,
2 skipped**; the newer main also contains the clock fixes for the older ranking
fixture failures recorded above.

Applied the provider response migration to TakeShapeApp (`pxnbfdsyuzweevkvremi`)
as remote version `20261013200344_provider_pdf_responses`. The live SQL check in
`backend/tests/sql/provider_pdf_responses.sql` passed RLS/grants, calendar-date
persistence, duplicate receipt rejection and cascade deletion, rolling back all
synthetic rows.

Browser verification uses a synthetic six-metre-wide room with known coordinates:

```sh
PYTHONPATH=backend .venv/bin/python backend/tests/browser/serve_quote_media_preview.py
# In a second terminal with Playwright installed and Chrome available:
node backend/tests/browser/quote_media_measurements.cjs
```

`PLAYWRIGHT_MODULE` can point to an existing Playwright package. The fixture binds
to localhost and disables production credentials. Tests use real pointer input
and check known metre distances, endpoint adjustments, multiple measurements,
sky rejection, orbit gestures, camera/photo coexistence, per-area restoration,
clear/cancel and a narrow viewport. `PROVIDER_SCREENSHOT` optionally saves a PNG.
