"""SOW §12 retention: purge flow-journal entries older than the configured
window (``LIDARAI_LOG_RETENTION_DAYS``, default 90), and optionally delete
all flow data for one homeowner (CCPA deletion path).

Usage (from backend/):

    .venv/Scripts/python scripts/retention_purge.py                 # purge by age
    .venv/Scripts/python scripts/retention_purge.py --homeowner ID  # delete one homeowner

Run the age purge on a schedule (e.g. Render cron) once deployed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


def main() -> None:
    if not (settings.supabase_url and settings.supabase_service_role_key):
        print("Supabase is not configured (LIDARAI_SUPABASE_URL / _SERVICE_ROLE_KEY).")
        sys.exit(1)
    client = httpx.Client(
        base_url=f"{settings.supabase_url.rstrip('/')}/rest/v1",
        headers={
            "apikey": settings.supabase_service_role_key,
            "Authorization": f"Bearer {settings.supabase_service_role_key}",
            "Content-Type": "application/json",
        },
        timeout=60,
    )
    if "--homeowner" in sys.argv:
        homeowner_id = sys.argv[sys.argv.index("--homeowner") + 1]
        resp = client.post(
            "/rpc/delete_flow_data_for_homeowner", json={"target": homeowner_id}
        )
        resp.raise_for_status()
        print(f"Deleted all flow data for homeowner {homeowner_id}.")
    else:
        days = settings.log_retention_days
        resp = client.post("/rpc/purge_flow_journal", json={"older_than_days": days})
        resp.raise_for_status()
        print(f"Purged {resp.json()} journal entries older than {days} days.")


if __name__ == "__main__":
    main()
