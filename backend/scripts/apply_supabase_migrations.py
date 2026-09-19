"""Apply the Supabase migrations to a project via the Management API (no
psql needed).

Migrations are collected from BOTH locations, deduped by filename:
- ``backend/supabase/migrations/`` — the flow-agent tables vendored with
  this backend (``flow_states``, ``flow_journal``, ``flow_quote_requests``),
  so the delivered repo can stand up its own persistence. These reference
  ``public.homeowners``, so the app's base schema must already exist on the
  target project (it does on any project the TakeShape app runs against).
- a sibling ``takeshape-mobile/supabase/migrations/`` checkout, when present
  (full app schema — used when provisioning a fresh dev project).

Usage (from backend/, with SUPABASE_ACCESS_TOKEN and SUPABASE_PROJECT_REF
in .env or the environment):

    .venv/Scripts/python scripts/apply_supabase_migrations.py [--dry-run]

Migrations run in filename order inside one call each; a `_migrations`
table records what has been applied so re-runs are idempotent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
MIGRATIONS_DIRS = [
    BACKEND / "supabase" / "migrations",
    BACKEND.parents[1] / "takeshape-mobile" / "supabase" / "migrations",
]

# Filename sort misorders the same-day 20260525 files: the policy fix and the
# provider-read policy both depend on quote_requests, which
# provider_profiles_quote_requests creates. Pin the intended order.
ORDER_OVERRIDES = {
    "20260525_provider_profiles_quote_requests.sql": 0,
    "20260525_fix_homeowners_quote_policy_recursion.sql": 1,
    "20260525_provider_can_read_assigned_homeowners.sql": 2,
}


def sort_key(path: Path) -> tuple:
    import re

    date_prefix = re.match(r"\d+", path.name)
    return (
        date_prefix.group(0) if date_prefix else path.name,
        ORDER_OVERRIDES.get(path.name, 0),
        path.name,
    )


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_path = BACKEND / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    import os

    for key in ("SUPABASE_ACCESS_TOKEN", "SUPABASE_PROJECT_REF"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def run_query(client: httpx.Client, ref: str, query: str) -> dict | list:
    resp = client.post(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        json={"query": query},
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"{resp.status_code}: {resp.text[:500]}")
    return resp.json() if resp.text else {}


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    env = load_env()
    token = env.get("SUPABASE_ACCESS_TOKEN")
    ref = env.get("SUPABASE_PROJECT_REF")
    if not token or not ref:
        print("SUPABASE_ACCESS_TOKEN and SUPABASE_PROJECT_REF are required (backend/.env)")
        sys.exit(1)
    by_name: dict[str, Path] = {}
    for directory in MIGRATIONS_DIRS:
        if directory.is_dir():
            for path in directory.glob("*.sql"):
                by_name.setdefault(path.name, path)
    files = sorted(by_name.values(), key=sort_key)
    if not files:
        print(f"No migrations found in {[str(d) for d in MIGRATIONS_DIRS]}")
        sys.exit(1)
    print(f"project={ref} migrations={len(files)} dry_run={dry_run}")

    with httpx.Client(
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=120,
    ) as client:
        run_query(
            client,
            ref,
            "create table if not exists public._migrations ("
            "name text primary key, applied_at timestamptz not null default now())",
        )
        # The ledger is reachable through PostgREST like any other public
        # table, and the anon key ships inside the iOS app -- so without
        # this anyone holding the app could read our migration history.
        # RLS on with no policies is the same service-role-only posture the
        # flow tables use; the service role bypasses RLS, so the migration
        # runner is unaffected. Supabase's advisor flags the table as
        # CRITICAL until this runs (seen on the dev project, Sep 18).
        run_query(
            client,
            ref,
            "alter table public._migrations enable row level security",
        )
        applied_rows = run_query(client, ref, "select name from public._migrations")
        applied = {row["name"] for row in applied_rows} if isinstance(applied_rows, list) else set()

        for path in files:
            if path.name in applied:
                print(f"  skip (applied)  {path.name}")
                continue
            if dry_run:
                print(f"  would apply     {path.name}")
                continue
            sql = path.read_text(encoding="utf-8")
            try:
                run_query(client, ref, sql)
                run_query(
                    client,
                    ref,
                    "insert into public._migrations (name) values ("
                    + json.dumps(path.name).replace('"', "'")
                    + ") on conflict do nothing",
                )
                print(f"  applied         {path.name}")
            except RuntimeError as exc:
                print(f"  FAILED          {path.name}: {exc}")
                print("Stopping; later migrations may depend on this one.")
                sys.exit(2)

    if not dry_run:
        tables = run_query(
            client := httpx.Client(
                headers={"Authorization": f"Bearer {token}"}, timeout=60
            ),
            ref,
            "select table_name from information_schema.tables "
            "where table_schema='public' order by table_name",
        )
        client.close()
        names = [row["table_name"] for row in tables]
        print(f"\npublic tables ({len(names)}): {', '.join(names)}")


if __name__ == "__main__":
    main()
