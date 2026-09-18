"""Tiny JSON file cache shared by the web-research modules.

Every cached record carries a ``fetchedAt`` epoch; reads past the TTL return
None. Fail-open by design: a cache problem must never break a lookup, so
errors log and fall through to a fresh fetch.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("lidarai.flow.cache")


def read_json_cache(path: Path, ttl_seconds: int) -> dict | None:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data.get("fetchedAt", 0)) > ttl_seconds:
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def write_json_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache write failed for %s: %s", path.name, exc)
