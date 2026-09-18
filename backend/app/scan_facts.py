"""The information ledger: what the scan already answered, and what may be said.

Ported from `takeshape/src/state.py`. Two ideas carry it:

  Gates decide WHETHER the agent may ask. The ledger decides WHAT.

Nothing is scripted -- the next move is the highest-value unknown the current
gate permits, so the conversation never reads as a form, and the ops package is
just the ledger rendered.

This module deliberately does **not** replace `app/flow/state.py`, which owns the
live ten-step machine, its turn lock and its enforcement. What it adds is the
part production has no equivalent of: a scan-sourced fact table, and code-side
gates on what a model's observations may be turned into.

**Why this is not prompt work.** `assertable_objects` and `statable_measurements`
decide what the agent is allowed to claim, in code, from the certainty the
two-classifier merge produced. A prompt that says "do not guess" is advice; a
filter that never hands over a `low`-certainty object is a guarantee. If the
model asserts something anyway it is contradicting context it was never given,
which is a far easier failure to catch than one it was merely asked to avoid.
"""

from __future__ import annotations

from typing import Any

COMPANION = "companion"
INTAKE = "intake"

# Facts a contractor needs before bidding. `required_for` keeps "ask only when it
# comes up" concrete: tenure matters for flooring, not for a feature wall.
# ponytail: a dict, not a rules engine. Add rows, not machinery.
FACTS: dict[str, tuple[str, tuple[str, ...], int]] = {
    #  key              source          required_for                     priority
    "job_type":        ("conversation", ("*",),                          10),
    "scope":           ("conversation", ("*",),                           9),
    "rooms":           ("conversation", ("*",),                           8),
    "materials":       ("conversation", ("painting", "flooring"),         6),
    "budget_range":    ("conversation", ("*",),                           5),
    "timeline":        ("conversation", ("*",),                           4),
    "tenure":          ("conversation", ("flooring", "structural"),       7),
    "occupied":        ("conversation", ("flooring", "structural"),       3),
    "property_type":   ("conversation", ("structural",),                  3),
    # Pre-filled from the scan. Never asked -- this is what the ingest bought us.
    "paintable_m2":    ("scan",         ("painting",),                     0),
    "floor_m2":        ("scan",         ("flooring",),                     0),
    "perimeter_m":     ("scan",         ("painting", "flooring"),          0),
    "wall_condition":  ("scan",         ("painting",),                     0),
    "floor_condition": ("scan",         ("flooring",),                     0),
}

# Customer-level facts live on TakeShape's user record, not with us. They survive
# across scans and projects; a zip does not change because someone rescanned.
CUSTOMER_FACTS = ("first_name", "zip", "address", "contact")

# Measurements come from geometry, so they are stateable as given. Anything the
# appearance pass produced is gated on the two-classifier merge instead.
SCAN_MEASUREMENTS = ("paintable_m2", "floor_m2", "perimeter_m")


def new_state(home_id: str, room_key: str = "") -> dict[str, Any]:
    return {
        "home_id": home_id,
        "room_key": room_key,
        "mode": COMPANION,
        "job_type": None,
        "facts": {},          # ledger entries that are known
        "declined": [],       # sticky within the project
        "asks": {},           # {key: count} -- never ask twice
        "pending_ask": None,  # the ask actually voiced last turn, awaiting a reply
        "scan": {"processing_complete": False, "rooms_covered": []},
        "quote_requested": False,
        "log": [],            # variant + correction events
    }


# --------------------------------------------------------------------------
# What the scan answered
# --------------------------------------------------------------------------


def prefill_from_context(state: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Anything the scan answers is never asked. Sources the ledger's `scan` rows.

    Measurements are taken verbatim because they came from geometry. Surface
    condition came from a model, so it is only recorded when the appearance pass
    actually saw the room -- a `geometry_only` document has no observations to
    record, and recording an empty one would suppress the question forever.
    """
    measurements = context.get("measurements") or {}
    for key in SCAN_MEASUREMENTS:
        value = measurements.get(key)
        if value is not None:
            state["facts"][key] = value

    if context.get("coverage") != "geometry_only":
        surfaces = context.get("surfaces") or {}
        if surfaces.get("walls"):
            state["facts"]["wall_condition"] = surfaces["walls"]
        if surfaces.get("floor"):
            state["facts"]["floor_condition"] = surfaces["floor"]

    room = context.get("room") or context.get("room_key")
    if room and room not in state["scan"]["rooms_covered"]:
        state["scan"]["rooms_covered"].append(room)
    return state


# --------------------------------------------------------------------------
# Code-side gates on what may be claimed
# --------------------------------------------------------------------------


def assertable_objects(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The only objects the agent may state as fact.

    `high` means RoomPlan geometry and the appearance pass independently agreed.
    `low` is a single unconfirmed observation and `unobserved` is geometry with
    no photo behind it; neither may be asserted, so neither is handed over.
    """
    return [
        obj
        for obj in context.get("objects") or []
        if obj.get("certainty") == "high"
    ]


def rescan_candidates(context: dict[str, Any]) -> list[str]:
    """Objects geometry found that no selected frame showed.

    An honest reason to offer a rescan -- something is demonstrably there and
    demonstrably unphotographed -- rather than a nudge dressed as helpfulness.
    """
    return [
        obj["class"]
        for obj in context.get("objects") or []
        if obj.get("certainty") == "unobserved" and obj.get("class")
    ]


def statable_measurements(context: dict[str, Any]) -> dict[str, float]:
    """Measurements are geometry, so all of them may be stated."""
    return dict(context.get("measurements") or {})


def agent_view(context: dict[str, Any]) -> dict[str, Any]:
    """The context as the model should receive it.

    Everything the agent must not assert is removed before the prompt is built,
    so a claim it cannot support is a claim it was never shown. This is the
    difference between asking a model to be careful and making carelessness
    impossible to express.
    """
    return {
        "room": context.get("room") or "",
        "objects": [
            {"class": obj["class"], "appearance": obj.get("appearance", "")}
            for obj in assertable_objects(context)
        ],
        "surfaces": dict(context.get("surfaces") or {}),
        "style": context.get("style") or "",
        "notable": list(context.get("notable") or []),
        "measurements": statable_measurements(context),
        "coverage": context.get("coverage", "unknown"),
    }


# --------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------


def required(job_type: str | None) -> set[str]:
    """Fact keys this job actually needs."""
    return {
        key
        for key, (_source, required_for, _priority) in FACTS.items()
        if "*" in required_for or (job_type and job_type in required_for)
    }


def missing(state: dict[str, Any]) -> set[str]:
    job = state.get("job_type")
    return {
        key
        for key in required(job)
        if key not in state["facts"] and key not in state["declined"]
    }


def next_ask(state: dict[str, Any]) -> str | None:
    """Highest-priority unknown, or None. Only meaningful in intake mode."""
    if state["mode"] != INTAKE:
        return None
    open_keys = [
        key
        for key in missing(state)
        if FACTS[key][0] == "conversation" and not state["asks"].get(key)
    ]
    if not open_keys:
        return None
    # Priority first, key second: two facts of equal priority must not depend on
    # set iteration order for which one gets asked.
    return max(open_keys, key=lambda key: (FACTS[key][2], key))


def record(state: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    state["facts"][key] = value
    if key == "job_type":
        state["job_type"] = value
    return state


def note_ask(state: dict[str, Any], key: str) -> dict[str, Any]:
    state["asks"][key] = state["asks"].get(key, 0) + 1
    return state


def decline(state: dict[str, Any], topic: str) -> dict[str, Any]:
    """Sticky. Re-raising a declined topic is the ham-fisted failure."""
    if topic not in state["declined"]:
        state["declined"].append(topic)
    return state


def enter_intake(state: dict[str, Any], job_type: str) -> dict[str, Any]:
    state["mode"] = INTAKE
    record(state, "job_type", job_type)
    return state


def may_discuss_services(state: dict[str, Any]) -> bool:
    return state["mode"] == INTAKE


def may_volunteer_condition(state: dict[str, Any], user_asked_opinion: bool) -> bool:
    """Never unprompted. The agent may call a room handsome; it may not call the
    carpet worn unless the homeowner asked what it thought."""
    return state["mode"] == INTAKE or user_asked_opinion


def may_request_address(state: dict[str, Any]) -> bool:
    return state["mode"] == INTAKE and not missing(state) - {"budget_range"}


def may_submit_quote(state: dict[str, Any], customer: dict[str, Any]) -> bool:
    return bool(
        state["mode"] == INTAKE
        and customer.get("address")
        and customer.get("contact")
        and not state["quote_requested"]
    )


def log_event(state: dict[str, Any], kind: str, **fields: Any) -> dict[str, Any]:
    state["log"].append({"kind": kind, **fields})
    return state
