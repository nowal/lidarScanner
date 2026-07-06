# Render Deploy Guide

This guide deploys the LiDAR processing backend as a single Render Web Service backed by a persistent disk. It is the fastest MVP path for real homeowner scans. The service is intentionally single-instance because the current backend stores job files on one local disk.

## What This Repo Provides

- `render.yaml` at the repo root for Render Blueprint setup.
- `backend/Dockerfile` with the Python and system packages needed for FastAPI, Pillow, and Open3D RGBD processing.
- `backend/.dockerignore` so local virtualenvs, secrets, and scan artifacts are not copied into the Docker build.
- Backend job rehydration from persisted disk records after Render restarts.
- Home AI chat endpoints backed by OpenAI Responses API calls over `httpx`, with local fallback responses when OpenAI is not configured.

## Before You Start

1. Push this repo to GitHub, GitLab, or Bitbucket.
2. Make sure these local-only paths are not committed:
   - `backend/.env`
   - `backend/.venv/`
   - `backend/backend_storage/`
3. Generate a backend token and keep it somewhere safe:

```bash
openssl rand -hex 32
```

You will paste this value into Render as `LIDARAI_AUTH_TOKEN` and into the iOS app's processor auth token field for MVP testing.

## Recommended Dashboard Flow: Blueprint

Starting from `https://dashboard.render.com`:

1. Click **New**.
2. Choose **Blueprint**.
3. Connect the Git provider that contains this repo, if Render asks.
4. Select the repo.
5. Use `render.yaml` as the Blueprint file path.
6. Review the service Render is about to create:
   - Name: `lidarai-processor`
   - Runtime: Docker
   - Instance type: `pro`
   - Region: `oregon`
   - Health check path: `/health`
   - Disk mount path: `/var/data`
   - Disk size: `20 GB`
7. When Render prompts for `LIDARAI_AUTH_TOKEN`, paste the token you generated.
8. When Render prompts for `LIDARAI_OPENAI_API_KEY`, paste an OpenAI API key if you want the Home Guide chat to use OpenAI in production.
   - Without an API key, `/api/v1/ai/home-chat` still returns the local fallback response shape.
   - Set `LIDARAI_OPENAI_ORGANIZATION` or `LIDARAI_OPENAI_PROJECT` later only if your OpenAI account/key routing requires them.
9. Apply/create the Blueprint.
10. Open the `lidarai-processor` service and watch the first deploy logs.

When deploy finishes, Render gives you a URL like:

```text
https://lidarai-processor.onrender.com
```

Verify:

```text
https://lidarai-processor.onrender.com/health
https://lidarai-processor.onrender.com/docs
```

`/health` should return JSON with `"status": "ok"`.

## Manual Dashboard Flow: Web Service

Use this if you do not want to use Blueprints.

1. Click **New**.
2. Choose **Web Service**.
3. Select the repo.
4. Set the service name to `lidarai-processor`.
5. Set **Language** to **Docker**.
6. Set **Dockerfile Path** to:

```text
backend/Dockerfile
```

7. Set **Docker Build Context Directory** to:

```text
backend
```

8. Select instance type **Pro** for the first real scans. You can try **Standard** to save money, but upgrade if the logs show memory kills or scans fail during Open3D/texturing.
9. Under **Advanced**, add a disk:
   - Mount path: `/var/data`
   - Size: `20 GB`
10. Add environment variables:

```text
LIDARAI_STORAGE_DIR=/var/data
LIDARAI_AUTH_TOKEN=<your generated token>
LIDARAI_CORS_ORIGINS=*
LIDARAI_JOB_TIMEOUT_SECONDS=1200
LIDARAI_DEFAULT_PROCESSING_PROFILE=fast_onboarding
LIDARAI_TEXTURE_WORKERS=2
LIDARAI_AI_PROVIDER=openai
LIDARAI_OPENAI_API_KEY=<your OpenAI API key>
LIDARAI_OPENAI_ORGANIZATION=
LIDARAI_OPENAI_PROJECT=
LIDARAI_OPENAI_MODEL=gpt-5.5
LIDARAI_OPENAI_FALLBACK_MODEL=
LIDARAI_OPENAI_REASONING_EFFORT=medium
LIDARAI_OPENAI_REQUEST_TIMEOUT_SECONDS=45
LIDARAI_OPENAI_MAX_IMAGES_PER_REQUEST=1
```

11. Set health check path to `/health`.
12. Click **Create Web Service**.

## Home AI And OpenAI Settings

The backend exposes:

```text
/api/v1/ai/home-chat
/api/v1/ai/home-events
```

Both endpoints use the same bearer token as processor jobs. The iOS app never receives the OpenAI API key.

| Variable | Render value | Notes |
| --- | --- | --- |
| `LIDARAI_AI_PROVIDER` | `openai` | Enables the OpenAI path when an API key exists. |
| `LIDARAI_OPENAI_API_KEY` | secret | Required for production OpenAI chat. Use `sync: false` in Blueprint setup. |
| `LIDARAI_OPENAI_ORGANIZATION` | blank by default | Optional. Set only when your OpenAI account requires an organization header. |
| `LIDARAI_OPENAI_PROJECT` | blank by default | Optional. Set only when your OpenAI key should be scoped to a project header. |
| `LIDARAI_OPENAI_MODEL` | `gpt-5.5` | Current default. As of July 3, 2026, OpenAI's latest-model guide recommends the `gpt-5.5` slug. Verify the official docs before changing it. |
| `LIDARAI_OPENAI_FALLBACK_MODEL` | blank by default | Optional lighter fallback model. Empty means the backend retries the primary model with fewer images, then uses local fallback. |
| `LIDARAI_OPENAI_REASONING_EFFORT` | `medium` | Balanced default for quality, latency, and cost. |
| `LIDARAI_OPENAI_REQUEST_TIMEOUT_SECONDS` | `45` | Timeout for the direct `httpx` call to `/v1/responses`. |
| `LIDARAI_OPENAI_MAX_IMAGES_PER_REQUEST` | `1` | Keeps Home Guide vision turns small on the Render web service. |

The backend calls OpenAI's Responses API directly with `httpx`; it does not need the OpenAI Python SDK. The Dockerfile copies `backend/app`, and the Home AI prompt, context, analytics, and tool modules all live under that package.

## Connect The iOS App

In the app's processor settings/results flow:

- Server URL: your Render URL, for example `https://lidarai-processor.onrender.com`
- Auth Token: the exact `LIDARAI_AUTH_TOKEN` you set in Render
- Home Guide chat uses the same server URL and auth token. Do not put the OpenAI key in the iOS app.

Run one small scan first, then a realistic room, then a whole-house scan.

## Operating Notes

- Disk storage is local to this one Render service. Keep `numInstances` at `1`.
- Render disks preserve only files under the mount path, so production storage must stay at `/var/data`.
- Processor jobs and Home AI state both persist under `LIDARAI_STORAGE_DIR`. On Render this includes job files plus `ai_threads/` and `ai_events/` under `/var/data`.
- `LIDARAI_TEXTURE_WORKERS=2` is a conservative Render Pro starting point for photoreal texture work. Increase only after upgrading the service size and watching memory during real scans.
- If jobs fail with memory errors, upgrade the service from `pro` to `pro plus`.
- If the disk fills, increase the disk size from the service's **Disks** tab. Render lets you increase disk size later, but not decrease it.
- A redeploy can interrupt an active processor run. The backend now reloads persisted job records after restart and marks interrupted running jobs as failed so the app can retry.
- Home AI conversations use OpenAI stored response state when available and local JSONL analytics on the Render disk. If OpenAI returns auth, quota, rate-limit, or transient errors, the endpoint logs the error and returns the local fallback response shape so the app can keep moving.

## MVP Security Note

This bearer token is good enough for controlled TestFlight/homeowner MVP testing, but it is not enough for a public App Store launch. Before broad public release, the processor should verify Supabase user JWTs, rate-limit jobs per user, and store inputs/results in Supabase Storage or S3-compatible object storage.
