# Walkthrough projects

The existing home UUID, authenticated intake, homeowner service decisions and
quote lifecycle now accept a walkthrough in the media slot. `HomeIndex.mediaType`
selects `scan` (default for existing indexes) or `walkthrough`. A walkthrough has a
private video reference and upload metadata. It has no storey count, model,
mesh bounds, measured dimensions or discovered rooms.

## Publication

After uploading the video through the existing private TUS and `home_assets`
ledger, the phone calls authenticated `POST /api/v1/ai/homes/{home}/uploads/walkthrough`:

```json
{
  "revision": "33333333-3333-3333-3333-333333333333",
  "generation": 1794900000000,
  "bucket": "metashape-exports",
  "objectPath": "<owner>/<home>/33333333-3333-3333-3333-333333333333.mov",
  "bytes": 12345678,
  "mimeType": "video/quicktime",
  "durationSeconds": 125.4
}
```

The same recording/revision is retried; a replacement gets a fresh revision and
monotonic generation. MOV/QuickTime and MP4 uploads are accepted; their extension
must match the declared MIME type and capture revision. The server verifies the
stored byte count, canonical owner path and project ownership. It does not
transcode or inspect video frames. The mobile recorder requests H.264.

`walkthrough_upload_transition` atomically publishes to the existing upload heads
and jobs. It is SECURITY INVOKER, service-role-only, and shares the existing RLS,
owner deletion and revision fencing. `videoReady=true` satisfies quote readiness;
`modelsReady` remains false. Old model readiness and scan publication rules remain.
An old quote keeps its original immutable video object after replacement.

No extra duration/byte cap is imposed by this route. Recording disk space and the
configured Storage upload limit are still real limits.

## Intake and provider delivery

Intake stores the media kind at its first turn. The small ownership ledger can
identify a video even between upload completion and publication. Neither the scan
nor video intake views the media. Video instructions collect the same service
choices, omit geometry questions and prevent visual claims. The provider brief
states that dimensions are unavailable. No invented zero measurements are saved.

The existing quote snapshot carries `video`; its private viewer switches the model
area to native video controls and hides model measurement controls. `/video`
authorizes the same expiring capability and redirects to a short-lived Storage
URL; Storage handles ranges and seeking. Service details, supplemental photos,
PDF responses and quote approvals remain the existing workflow.

## Rollout and checks

Companion mobile branch: `codex/walkthrough-video-first-pass` in `nowal/takeshape-mobile`
(local canonical checkout `/Users/noahwaldron/Documents/code/takeshape-mobile`).
Backend branch: `codex/walkthrough-backend`, based on `9438312`.
Apply `20261117174552_walkthrough_project_media.sql`, deploy backend, then release
mobile. This review did not apply a production migration, deploy, send email, or
create a live quote request.

Run from `backend` with the existing virtualenv:

```sh
python -m pytest tests/test_walkthroughs.py tests/test_direct_quote_requests.py tests/test_staged_scan_uploads.py tests/test_quote_media.py tests/test_quote_intake.py tests/test_quote_intake_recovery_latency.py -q
PGLITE_MODULE=/path/to/@electric-sql/pglite node tests/sql/walkthrough_uploads.cjs
```

The API regression group has 184 passing tests. SQL checks execute the actual
migration, test ownership/retry/replacement fencing and permissions, and run the
existing scan-transition suite. Browser fixtures in `tests/browser` disable all
outbound credentials and verify real video playback/seeking through the viewer.
The full backend run had 1532 passing tests, 2 skipped, and 3 pre-existing email
copy assertion failures; all three reproduce on unchanged `9438312`.

Physical non-LiDAR camera capture and a real account upload against the migrated
backend remain release smoke checks. The mobile saved-video library remains local;
this change adds durable server media and chat, not a cross-device download screen.
