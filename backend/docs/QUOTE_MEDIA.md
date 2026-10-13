# Quote submission media

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
only applied to quote-request lead messages, including explicit resends; decision
emails and reply confirmations keep their existing destinations. Resend and SMTP
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
