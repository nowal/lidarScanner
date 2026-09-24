# Staged mobile uploads

The phone uploads a small context ZIP after the scan is filed, alongside local texturing. Once the model is saved, it uploads each area's USDZ and the whole-home USDZ directly to `metashape-exports/<homeowner-id>/<scan-id>/`. Every upload is recorded in `home_assets`. No schema or Storage policy change is needed.

The saved scan UUID remains the Home Guide home ID. New routes require both the service bearer token and a verified Supabase guest/account in `X-Homeowner-Token`. Paths must belong to that homeowner and scan. Existing database upload records establish ownership when upgrading a legacy home index.

- `POST /api/v1/ai/homes/{homeId}/uploads/context`: `{bucket, objectPath, revision, enrich}`. Queues ingestion. Duplicate requests reuse the operation; a competing object returns 409.
- `GET /api/v1/ai/homes/{homeId}/uploads/context?objectPath=...`: returns `queued`, `running`, `done` with `roomCount`, `failed` with `error`, or `unknown` after an interrupted server restart. Completed status is recovered from the durable home index.
- `POST /api/v1/ai/homes/{homeId}/uploads/models`: `{revision, models: [{key, bucket, objectPath, bytes}]}`. Requires `home` and every `room-N`, verifies Storage sizes, and saves links without rebuilding the room index. Completion means the index was saved durably.

Context contains `meta.json`, per-area `room.json`, `floor.json`, `rebuild/manifest.json`, and up to four JPEGs with a 768-pixel maximum long edge. `aiSelectionVersion: 1` and `aiSelectedFrameIds` preserve the phone's choices. The backend corrects native image orientation from camera gravity. Empty cloud exports fail visibly.

Cold home-index reads use a fresh `cacheNonce` so a restarted worker cannot retain a cached pre-model snapshot. Live verification observed a CDN HIT after a successful model registration; an origin read confirmed the saved model references. Supabase documents this [cache bypass](https://supabase.com/docs/guides/storage/cdn/smart-cdn#bypassing-cache) for mutable objects.

Quote submission waits for `upload.modelsReady`. Email links prefer the room under discussion, then the whole home. New records specify their Storage bucket; legacy `home-models/...` links still work. Requeued emails refresh model links before sending. Forgetting a staged home removes its source uploads and database asset rows along with the derived index.

## Email setup

Noah selected Nathan's Resend account. Live delivery still requires Nathan's private key, an allowed sender, and the intended recipient(s): `LIDARAI_RESEND_API_KEY`, `LIDARAI_OPS_EMAIL_FROM`, and `LIDARAI_OPS_EMAIL`. Configure these together privately in Render; never commit them. Verification did not send emails.

Disk capture now sets `opsEmailCapturedAt`; only transport success sets `opsEmailDeliveredAt`. Captured leads requeue when transport is available. Legacy false delivery stamps are recoverable when the disk outbox file survives. Historical leads whose outbox evidence was lost on Render's ephemeral disk must be identified by an operator before replay; do not resend all history blindly.

## Verification

Tests cover early ingestion, late model merging, ownership, foreign objects/buckets, stale revisions, incomplete models, durable-write failure, restart status, empty exports, photo selection, cleanup, model links, and outbox retry. A real Supabase probe used two areas/eight photos, three USDZs including a 7 MB chunked upload, a downloadable signed room-model link, and a second guest rejected with HTTP 403. Test users, database assets, and Storage objects were removed afterward. Physical LiDAR scanning and iOS background suspension still need device testing.
