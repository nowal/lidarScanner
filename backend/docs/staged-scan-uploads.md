# Staged mobile uploads

The phone uploads a small context ZIP after the scan is filed, alongside local texturing. Once the model is saved, new jobs upload one losslessly compressed whole-home USDZ to `metashape-exports/<homeowner-id>/<scan-id>/home-<sha256>.usdz.gz`. The complete home already embeds every area's USDZ. The backend restores those exact bytes and provides native whole-home and area downloads. Every upload is recorded in `home_assets`. Automatic files use a SHA-256 in their immutable object name; the asset id is deterministic, so retrying a completed transfer safely finishes its ledger entry. Storage remains insert-only for the phone. Saved jobs from older apps continue their original individual-area plus whole-home uploads.

Apply both `20260930163532_crash_safe_scan_uploads.sql` and `20260930164959_scan_upload_revision_context.sql` before deploying this backend. They add service-role-only `scan_upload_heads` / `scan_upload_jobs` tables and an invoker RPC. RLS blocks client access; ownership continues to be verified by the existing API. The SQL tests in `tests/sql/scan_upload_transitions.sql` exercise real database transitions within a rolled-back transaction.

The saved scan UUID remains the Home Guide home ID. New routes require both the service bearer token and a verified Supabase guest/account in `X-Homeowner-Token`. Paths must belong to that homeowner and scan. Existing database upload records establish ownership when upgrading a legacy home index.

- `POST /api/v1/ai/homes/{homeId}/uploads/context`: `{bucket, objectPath, revision, generation?, enrich}`. Reserves a durable revision and leases ingestion to one worker. Retries reuse the job. A higher client generation supersedes an older build; a stale build gets a non-retryable `revision_mismatch`. Legacy clients without a generation can retry their current job but cannot supersede a revision started by a new client.
- `GET /api/v1/ai/homes/{homeId}/uploads/context?objectPath=...`: returns `queued`, `running`, `done` with `roomCount`, `failed` with `error`, or `unknown` after a worker lease expires (resubmit the same object). An older object returns `superseded`. Status comes from the shared database, across workers and restarts.
- `POST /api/v1/ai/homes/{homeId}/uploads/models`: `{revision, models: [{key, bucket, objectPath, bytes}]}`. Requires `home` and every `room-N`, verifies Storage sizes, and saves links without rebuilding the room index. Completion means the database atomically switched the complete published model set after verifying every object. The revision is checked again inside that transaction.
- `POST /api/v1/ai/homes/{homeId}/uploads/home-model`: `{revision, model: {key: "home", bucket, objectPath, bytes, encoding: "gzip", uncompressedBytes, sha256}}`. Starts or resumes the compressed model preparation job and returns HTTP 202. The filename hashes the compressed bytes; `sha256` identifies the original USDZ. `GET` on the same route with `?revision=...` reports durable progress. `unknown` means its lease expired; repeat POST with the identical payload.

## Whole-home compression

Apply `20261001135238_compressed_home_uploads.sql` before deploying the compressed-model routes, then release the new phone app. It adds an independent model lease and saved resumable-upload sessions to the existing revision job. Previous client routes remain supported. Run `tests/sql/compressed_home_transitions.sql` to verify lease recovery and atomic publication in a rolled-back transaction.

Compression streams through zlib on the phone without changing the local model. The backend verifies both compressed and uncompressed hashes and sizes, requires every context area in the archive, and extracts only fixed `rooms/room-N/model.usdz` entries. The original USDZ size limit remains 1,048,576,000 bytes. Models use six-MiB TUS chunks; saved server upload URLs survive restarts. Repeating a completed request returns `done` without decompressing again. A retry after a server failure resumes preparation without retransmitting the phone's archive.

Only after every native model is uploaded does one database transaction publish the whole set and its `home_assets` records. The uploaded gzip uses asset type `scan_model_archive`; it cannot be mistaken for the native `lidar_model`. Room quote links continue to use `scan_room_model` objects. Older published models remain available throughout preparation. Gzip transport objects are retained for recovery and removed with the scan; this reduces phone bandwidth, not retained cloud storage or the native USDZ's size.

The October 1 byte-level check used a 448,600,370-byte real home: native Swift compression produced 195,800,930 bytes, versus 897,199,865 bytes previously uploaded for home plus areas. Backend decompression restored the full home and all three area packages byte for byte. Geometry, UVs, textures and image encodings are unchanged.

Context polling includes `stage` (`reading_scan`, `analyzing_photos`, `saving_context`, `ready`), photo-analysis `completedRooms`/`totalRooms`, and the scan revision. Readiness comes from the saved index, not just a finished background-task flag. A slow Storage read cannot overwrite a newer index published while that read was in flight. Final model registration refreshes a stale local index before rejecting it; temporary 409s have `detail.code` of `context_not_ready` or `context_ingesting` and `retryable: true`. A real `revision_mismatch` is not retryable as the old scan.

Context contains `meta.json`, per-area `room.json`, `floor.json`, `rebuild/manifest.json`, and up to four JPEGs with a 768-pixel maximum long edge. `aiSelectionVersion: 1` and `aiSelectedFrameIds` preserve the phone's choices. The backend corrects native image orientation from camera gravity. Empty cloud exports fail visibly.

Legacy home-index reads use a fresh `cacheNonce` so a restarted worker cannot retain a cached pre-model snapshot. Live verification observed a CDN HIT after a successful model registration; an origin read confirmed the saved model references. Supabase documents this [cache bypass](https://supabase.com/docs/guides/storage/cdn/smart-cdn#bypassing-cache) for mutable objects.

Quote submission waits for `upload.modelsReady`. Email links prefer the room under discussion, then the whole home. New records specify their Storage bucket; legacy `home-models/...` links still work. Requeued emails refresh model links before sending. Forgetting a staged home removes its source uploads and database asset rows along with the derived index.

## Crash, rebuild, and update behavior

The phone persists `cloud-upload.json`, immutable `cloud-uploads/<revision>/` snapshots, TUS upload URLs, and a `cloud-model-ready.txt` handoff written before the bake marker is released. Relaunch continues the same upload job and resumes accepted chunks. An expired session checks the content-addressed object before creating another upload. A new rebuild refreshes lightweight context if final uploading never started; after that it can reuse valid context. Updates with new capture always refresh it. Every explicit rebuild gets fresh model receipts/snapshots. Transient network/server errors retry with bounded backoff; foregrounding or Retry continues saved work.

Vlad's area/region checkpoints remain independent of uploads. Partial bake results are kept for local viewing but are not published as a successful replacement. A crash before the bake marker is created also resumes the intended job on launch.

Each backend worker reads authoritative state from Postgres. A 120-second lease is renewed every 25 seconds; a reclaimed worker and a superseded revision cannot publish. Pending context and the last published model set are separate. Context can reach chat early, with new quote submission waiting for its models; old published files remain downloadable until the replacement completes. Previous context preserves homeowner names even before the first completed model set. Old objects are retained for existing links and are removed by the scan deletion path, not while replacing models. Legacy Storage indexes remain readable; managed revisions live in the database and do not rely on overwriting cached JSON.

## Email setup

Noah selected Nathan's Resend account. Live delivery still requires Nathan's private key, an allowed sender, and the intended recipient(s): `LIDARAI_RESEND_API_KEY`, `LIDARAI_OPS_EMAIL_FROM`, and `LIDARAI_OPS_EMAIL`. Configure these together privately in Render; never commit them. Verification did not send emails.

Disk capture now sets `opsEmailCapturedAt`; only transport success sets `opsEmailDeliveredAt`. Captured leads requeue when transport is available. Legacy false delivery stamps are recoverable when the disk outbox file survives. Historical leads whose outbox evidence was lost on Render's ephemeral disk must be identified by an operator before replay; do not resend all history blindly.

## Verification

Tests cover early ingestion, late model merging, ownership, foreign objects/buckets, stale revisions, incomplete models, durable-write failure, restart status, empty exports, photo selection, cleanup, model links, and outbox retry. A real Supabase probe used two areas/eight photos, three USDZs including a 7 MB chunked upload, a downloadable signed room-model link, and a second guest rejected with HTTP 403. Test users, database assets, and Storage objects were removed afterward. Physical LiDAR scanning and iOS background suspension still need device testing.

The September 30 checks also cover saved TUS sessions, missed bake callbacks, immutable retry snapshots, two concurrent PostgREST lease requests, stale completion fencing, and an isolated live Storage/database replacement whose previous model stayed downloadable. Synthetic rows and objects were removed; no emails were sent. Device memory-pressure/LiDAR crash testing remains a physical-device check.
