# Render Deploy Guide

This guide deploys the LiDAR processing backend as a single Render Web Service backed by a persistent disk. It is the fastest MVP path for real homeowner scans. The service is intentionally single-instance because the current backend stores job files on one local disk.

## What This Repo Provides

- `render.yaml` at the repo root for Render Blueprint setup.
- `backend/Dockerfile` with the Python and system packages needed for FastAPI, Pillow, and Open3D RGBD processing.
- `backend/.dockerignore` so local virtualenvs, secrets, and scan artifacts are not copied into the Docker build.
- Backend job rehydration from persisted disk records after Render restarts.

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
8. Apply/create the Blueprint.
9. Open the `lidarai-processor` service and watch the first deploy logs.

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
```

11. Set health check path to `/health`.
12. Click **Create Web Service**.

## Connect The iOS App

In the app's processor settings/results flow:

- Server URL: your Render URL, for example `https://lidarai-processor.onrender.com`
- Auth Token: the exact `LIDARAI_AUTH_TOKEN` you set in Render

Run one small scan first, then a realistic room, then a whole-house scan.

## Operating Notes

- Disk storage is local to this one Render service. Keep `numInstances` at `1`.
- Render disks preserve only files under the mount path, so production storage must stay at `/var/data`.
- If jobs fail with memory errors, upgrade the service from `pro` to `pro plus`.
- If the disk fills, increase the disk size from the service's **Disks** tab. Render lets you increase disk size later, but not decrease it.
- A redeploy can interrupt an active processor run. The backend now reloads persisted job records after restart and marks interrupted running jobs as failed so the app can retry.

## MVP Security Note

This bearer token is good enough for controlled TestFlight/homeowner MVP testing, but it is not enough for a public App Store launch. Before broad public release, the processor should verify Supabase user JWTs, rate-limit jobs per user, and store inputs/results in Supabase Storage or S3-compatible object storage.

