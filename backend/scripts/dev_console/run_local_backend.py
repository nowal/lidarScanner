"""Run the backend locally for the demo pages, with an explicit environment.

Copies only the credentials the demo needs out of backend/.env (the model
key and the Supabase project, so ingested homes and the provider table are
available), blanks every outbound mail transport so a submitted lead is
captured under the storage directory instead of sent, and keeps the
homeowner endpoints open on localhost. Never point a phone at this over
the internet: it is unauthenticated by design for a screen-shared demo.

    .venv/Scripts/python scripts/dev_console/run_local_backend.py [--port 8010]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1]
STORAGE = BACKEND / "backend_storage" / "demo_storage"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--signal", default="processor_job", choices=["processor_job", "device_bake"])
    args = parser.parse_args()

    env: dict[str, str] = {}
    env_path = BACKEND / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    for key in ("LIDARAI_ANTHROPIC_API_KEY", "LIDARAI_ANTHROPIC_EFFORT", "LIDARAI_OPENAI_API_KEY",
                "LIDARAI_SUPABASE_URL", "LIDARAI_SUPABASE_SERVICE_ROLE_KEY", "LIDARAI_SUPABASE_JWT_SECRET"):
        if key in env:
            os.environ[key] = env[key]

    os.environ.update({
        "LIDARAI_AI_PROVIDER": os.environ.get("LIDARAI_AI_PROVIDER", "anthropic"),
        "LIDARAI_STORAGE_DIR": str(STORAGE),
        "LIDARAI_OPS_TOKEN": os.environ.get("LIDARAI_OPS_TOKEN", "e2e-ops-token"),
        "LIDARAI_OPS_EMAIL": "ops-demo@example.com",
        "LIDARAI_RESEND_API_KEY": "",
        "LIDARAI_SMTP_HOST": "",
        "LIDARAI_OPS_WEBHOOK_URL": "",
        "LIDARAI_PUBLIC_BASE_URL": f"http://127.0.0.1:{args.port}",
        "LIDARAI_FLOW_TOKEN_SECRET": "local-demo-secret",
        "LIDARAI_PROVIDER_FINDER_ENABLED": "false",
        "LIDARAI_PROVIDER_DISCOVERY_ENABLED": "false",
        "LIDARAI_SCAN_COMPLETE_SIGNAL": args.signal,
        "LIDARAI_STRICT_CONFIG": "false",
    })
    # Run from a directory without a .env so nothing else leaks in.
    os.chdir(HERE)
    sys.path.insert(0, str(BACKEND))
    import uvicorn

    print(f"Home Guide local backend on http://127.0.0.1:{args.port} (storage {STORAGE})", flush=True)
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
