"""Repeat-trial reliability harness (pattern adopted from the
firefightersafe-api validation suite): run the full 10-step acceptance
rehearsal N times and report per-check pass RATES, separating always-passing
from flaky from always-failing.

This is the seed of the Sprint 2 evaluation suite and the honest way to talk
about conversation reliability — a single green run hides flakiness that
N runs expose. Each trial is a fresh thread against the live model and live
Supabase (~10 model turns per trial).

Usage (from backend/):

    .venv/Scripts/python scripts/reliability_harness.py [trials]   # default 3
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.acceptance_rehearsal import run_rehearsal  # noqa: E402


async def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    results: dict[str, list[bool]] = defaultdict(list)
    durations: list[float] = []
    run_stamp = int(time.time())
    for index in range(trials):
        started = time.monotonic()
        # The stamp keeps threads unique across invocations — the opening
        # endpoint is idempotent per thread (durably, via Supabase), so a
        # reused id would replay a previous run's opener.
        checks = await run_rehearsal(quiet=True, thread_suffix=f"trial-{run_stamp}-{index}")
        durations.append(time.monotonic() - started)
        for name, ok in checks:
            results[name].append(ok)
        passed = sum(1 for _, ok in checks if ok)
        print(f"trial {index + 1}/{trials}: {passed}/{len(checks)} in {durations[-1]:.0f}s")

    print(f"\n{'check':<58}{'pass':>6}{'rate':>7}  verdict")
    print("-" * 80)
    stable, flaky, broken = 0, 0, 0
    for name, outcomes in results.items():
        rate = sum(outcomes) / len(outcomes)
        if rate == 1.0:
            verdict = "stable"
            stable += 1
        elif rate == 0.0:
            verdict = "ALWAYS FAILING"
            broken += 1
        else:
            verdict = "FLAKY"
            flaky += 1
        print(f"{name:<60}{sum(outcomes)}/{len(outcomes):>3}{rate:>7.0%}  {verdict}", flush=True)

    total_rate = sum(sum(o) for o in results.values()) / sum(len(o) for o in results.values())
    print(
        f"\n{trials} trials, ~{sum(durations) / len(durations):.0f}s each | "
        f"overall check pass rate {total_rate:.0%} | "
        f"{stable} stable, {flaky} flaky, {broken} always-failing"
    )
    sys.exit(0 if broken == 0 and flaky == 0 else 1)


asyncio.run(main())
