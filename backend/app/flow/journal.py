"""The per-step wording journal (SOW §4: "a record of which wording the agent
used at each step and how the user responded").

One structured JSON line per turn — including turns where enforcement
suppressed a draft (the suppressed text is journaled, masked, so tuning can
see what the model *wanted* to say). Written locally now; the record shape is
already aligned with the `ai_messages`/`ai_events` Supabase tables so the
Week-2 persistence layer is a sink swap, not a redesign.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import now_utc
from .pii import mask_text
from .state import FlowState

logger = logging.getLogger("lidarai.flow.journal")


@dataclass
class TurnJournalEntry:
    thread_id: str
    step: int
    step_name: str
    wording_ids: list[str] = field(default_factory=list)
    user_text: str = ""
    agent_text: str = ""
    suppressed_drafts: list[dict[str, Any]] = field(default_factory=list)  # {text, violations}
    gates: dict[str, Any] = field(default_factory=dict)
    gate_reasons: dict[str, str] = field(default_factory=dict)
    slots_delta: dict[str, Any] = field(default_factory=dict)
    homeowner_id: str | None = None
    model: str = ""
    prompt_version: str = ""
    prompt_variant: str = ""
    used_fallback: bool = False
    latency_ms: int | None = None
    kind: str = "chat"  # chat | opening | quote_request | results_presented | blocked


def write_turn(
    storage_dir: str,
    entry: TurnJournalEntry,
    state: FlowState,
    *,
    mask_pii: bool = True,
) -> dict[str, Any] | None:
    """Append one journal line locally and return the masked record (the
    caller forwards it to durable storage). Never raises — journaling must
    not break a turn."""
    try:
        slots = state.slots if mask_pii else None
        record = {
            "recordVersion": 1,
            "createdAt": now_utc().isoformat(),
            "kind": entry.kind,
            "threadId": entry.thread_id,
            "homeownerId": entry.homeowner_id,
            "step": entry.step,
            "stepName": entry.step_name,
            "wordingIds": entry.wording_ids,
            "userText": mask_text(entry.user_text, slots) if mask_pii else entry.user_text,
            "agentText": mask_text(entry.agent_text, slots) if mask_pii else entry.agent_text,
            "suppressedDrafts": [
                {
                    "text": mask_text(d.get("text", ""), slots) if mask_pii else d.get("text", ""),
                    "violations": d.get("violations", []),
                }
                for d in entry.suppressed_drafts
            ],
            "gates": entry.gates,
            "gateReasons": entry.gate_reasons,
            "slotsDelta": _mask_slots_delta(entry.slots_delta) if mask_pii else entry.slots_delta,
            "flow": {
                "userTurns": state.user_turns,
                "completedSteps": state.completed_steps,
                "zipWordingId": state.zip_wording_id,
                "scopeWordingId": state.scope_wording_id,
                "scopeIntent": str(state.scope_intent),
                "scopeRooms": list(state.scope_rooms),
                "extensionOffers": state.extension_offers,
                "scanState": str(state.scan.state),
                "scanProcessorState": str(state.scan.processor),
                "scanServerVerified": state.scan.server_verified,
                "scanSignal": state.scan.signal,
            },
            "model": entry.model,
            "promptVersion": entry.prompt_version,
            "promptVariant": entry.prompt_variant,
            "usedFallback": entry.used_fallback,
            "latencyMs": entry.latency_ms,
        }
        line = json.dumps(record, ensure_ascii=True) + "\n"
        base = Path(storage_dir) / "flow_journal"
        base.mkdir(parents=True, exist_ok=True)
        safe_thread = re.sub(r"[^A-Za-z0-9_-]", "_", entry.thread_id)
        with (base / f"{safe_thread}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line)
        with (base / "journal.jsonl").open("a", encoding="utf-8") as f:
            f.write(line)
        return record
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write flow journal entry: %s", exc)
        return None


def _mask_slots_delta(delta: dict[str, Any]) -> dict[str, Any]:
    """The delta records *that* a value was captured, never sensitive values."""
    masked = dict(delta)
    for key in ("address", "contactEmail", "contactPhone", "firstName"):
        if masked.get(key):
            masked[key] = "[captured]"
    return masked


def read_thread_journal(storage_dir: str, thread_id: str) -> list[dict[str, Any]]:
    safe_thread = re.sub(r"[^A-Za-z0-9_-]", "_", thread_id)
    path = Path(storage_dir) / "flow_journal" / f"{safe_thread}.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries
