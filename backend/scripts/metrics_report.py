"""Summarize request metrics: per-class count, p50/p95/max latency, request
sizes, and RSS trend. This report is the substance behind the Render
capacity recommendation.

Usage: .venv/Scripts/python scripts/metrics_report.py [path-to-requests.jsonl]
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings  # noqa: E402


def pct(sorted_values: list[int], p: float) -> int:
    if not sorted_values:
        return 0
    idx = min(len(sorted_values) - 1, int(round(p * (len(sorted_values) - 1))))
    return sorted_values[idx]


def main() -> None:
    path = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(settings.storage_dir) / "metrics" / "requests.jsonl"
    )
    if not path.exists():
        print(f"No metrics at {path}")
        sys.exit(1)
    by_class: dict[str, list[dict]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        by_class[record.get("class", "other")].append(record)

    header = f"{'class':<14}{'n':>6}{'p50ms':>8}{'p95ms':>8}{'maxms':>8}{'err%':>7}{'maxReqMB':>10}"
    print(header)
    print("-" * len(header))
    for name in sorted(by_class, key=lambda k: -len(by_class[k])):
        rows = by_class[name]
        durations = sorted(r["durationMs"] for r in rows)
        errors = sum(1 for r in rows if r["status"] >= 500)
        max_req = max((r.get("requestBytes") or 0) for r in rows) / 1_048_576
        print(
            f"{name:<14}{len(rows):>6}{pct(durations, 0.5):>8}{pct(durations, 0.95):>8}"
            f"{durations[-1] if durations else 0:>8}{100 * errors / len(rows):>6.1f}%"
            f"{max_req:>9.2f}M"
        )
    rss = [r["rssMb"] for rows in by_class.values() for r in rows if r.get("rssMb")]
    if rss:
        print(f"\nRSS MB: first={rss[0]} max={max(rss)} last={rss[-1]}")


if __name__ == "__main__":
    main()
