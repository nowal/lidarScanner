"""Conversational memory, and the room a lead is actually for.

Both come straight out of the 30-persona battery (Sep 3):

- The agent re-asked for a zip that had just been given, told a homeowner
  it had "no record" of a colour she named one turn earlier, and once
  recited the scanned room's geometry as a summary of a conversation that
  never happened. The captured slots existed the whole time; they were
  never put in front of the model.
- It also would not shorten up for someone answering in three words.
- And with whole-home scans live, a quote request carried no room at all,
  so a lead from a 19-room house read "Painting, 37203".
"""

import json

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow.home_registry import _cache, ingest_bundle
from app.flow.state import FlowState, Slots
from app.flow_quotes import _room_measurements, create_quote_request
from app.home_ai import HomeAIChatRequest

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def _directives(state: FlowState, message: str = "hi") -> str:
    plan = flow_runtime._engine.plan_turn(state, message)
    return flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None, quotes_to_present=None
    )


def _captured() -> FlowState:
    return FlowState(
        thread_id="t",
        user_turns=5,
        slots=Slots(
            first_name="Dana", zip="37203", project_type="Painting",
            scope_options=["walls and trim"], materials=["low-VOC paint"],
        ),
    )


# ------------------------------------------------------------------- memory
def test_captured_facts_are_put_in_front_of_the_model():
    text = _directives(_captured())
    assert "ALREADY ESTABLISHED" in text
    for fact in ("Dana", "37203", "Painting", "walls and trim", "low-VOC paint"):
        assert fact in text, f"{fact} was captured but never shown to the model"


def test_the_ledger_forbids_re_asking_and_denying():
    text = _directives(_captured())
    assert "never ask for them again" in text
    assert "never say you have no record of them" in text.replace("\n", " ")


def test_memory_discipline_is_always_present():
    """Even with nothing captured yet, the agent must not invent history."""
    text = _directives(FlowState(thread_id="t"))
    assert "MEMORY DISCIPLINE" in text
    assert "never invent an earlier exchange" in text
    assert "ALREADY ESTABLISHED" not in text


def test_remembered_facts_are_checked_not_just_accepted():
    """The first version of this rule said "believe them" flatly, and the
    False Memory persona (Sep 4) got every invented history affirmed with
    "You're right". Agreeing to a fact is fine; pretending to recall it is
    not."""
    text = _directives(_captured())
    assert "CHECK the conversation above" in text
    assert "never claim you have no record of it" in text
    assert "never open with 'you're right' about something you cannot find" in text
    assert "pretending to remember it is not" in text


def test_recap_may_only_use_the_real_conversation():
    """The Restarter persona got a recap describing a room she never
    mentioned, built from the scan rather than the conversation."""
    text = _directives(_captured())
    assert "summarise ONLY what was actually discussed" in text
    assert "never present the room's measurements" in text.lower()


def test_invented_specifics_are_banned():
    """Freebie Seeker pressured it into naming a specific bed frame model."""
    text = _directives(_captured())
    assert "no brand names" in text
    assert "SKU" in text or "product" in text


# ------------------------------------------------------------ energy matching
def test_short_answers_get_a_short_reply_instruction():
    """Averaging three words is telegraphic, so it gets the tight cap; the
    looser one is covered in test_dead_ends."""
    state = _captured()
    state.recent_user_words = [3, 2, 4]
    assert "ONE OR TWO WORDS" in _directives(state)


def test_normal_length_answers_do_not():
    state = _captured()
    state.recent_user_words = [24, 31, 18]
    assert "MATCH THAT" not in _directives(state)


def test_one_short_answer_is_not_a_pattern():
    state = _captured()
    state.recent_user_words = [4]
    assert "MATCH THAT" not in _directives(state)


@pytest.mark.asyncio
async def test_word_counts_are_recorded_per_turn(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = FlowState(thread_id="t")
    for message in ("paint", "idk maybe blue", "sure"):
        flow_runtime._reconcile_home(state, HomeAIChatRequest(threadId="t", message=message))
        state.recent_user_words.append(len(message.split()))
    assert state.recent_user_words == [1, 3, 1]


# -------------------------------------------------------- room in the lead
@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    _cache.clear()
    bundle = tmp_path / "bundle"
    (bundle / "rooms").mkdir(parents=True)
    (bundle / "meta.json").write_text("{}", encoding="utf-8")
    for i, (label, w, d, objs, wins) in enumerate([
        ("bathroom", 3.5, 3.5, ["bathtub", "sink", "sink", "toilet"], 2),
        ("kitchen", 6.0, 5.0, ["sink", "stove"], 1),
    ], start=1):
        room_dir = bundle / "rooms" / f"room-{i}"
        (room_dir).mkdir(parents=True, exist_ok=True)
        (room_dir / "floor.json").write_text(json.dumps({"floor": 1, "floorY": 0.0}), encoding="utf-8")
        t = list(IDENTITY)
        t[12] = i * 20
        (room_dir / "room.json").write_text(json.dumps({
            "sections": [{"label": label}],
            "floors": [{"polygonCorners": [[-w/2, 0, -d/2], [w/2, 0, -d/2], [w/2, 0, d/2], [-w/2, 0, d/2]],
                        "transform": t}],
            "objects": [{"category": {o: {}}} for o in objs],
            "windows": [{"category": "window"} for _ in range(wins)],
            "doors": [], "walls": [], "openings": [],
        }), encoding="utf-8")
    yield ingest_bundle(bundle, "h1")
    _cache.clear()


def test_the_lead_carries_the_room_not_the_whole_house(home):
    state = _captured()
    state.home_id = "h1"
    state.active_room_key = "room-1"
    measurements, key, name = _room_measurements(state)
    # One bathroom in the house, so it is simply "bathroom" -- "primary"
    # is a distinction only worth drawing when there is more than one.
    assert name == "bathroom" and key == "room-1"
    assert measurements["room"] == "bathroom"
    # The bathroom, not the sum of the house.
    assert 100 < measurements["floorAreaSquareFeet"] < 150
    assert measurements["windowCount"] == 2
    assert any("bathtub" in f for f in measurements["fixtures"])


@pytest.mark.asyncio
async def test_quote_request_records_the_room(home, monkeypatch):
    state = _captured()
    state.home_id = "h1"
    state.active_room_key = "room-2"
    state.slots.address = "1 Test St"
    state.slots.contact_email = "d@example.com"
    record = await create_quote_request(
        state, thread_id="t", measurements={"note": "whole house", "floorAreaSquareFeet": 3356},
        quote_draft=None,
    )
    assert record.roomName == "kitchen"
    assert record.homeId == "h1"
    # The whole-house measurements must not survive into the lead.
    assert record.measurements["floorAreaSquareFeet"] < 500
    assert "kitchen" in record.ops_view()["project"]["room"]
    assert "kitchen" in record.synopsis


@pytest.mark.asyncio
async def test_single_room_captures_keep_the_context_measurements(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = _captured()
    state.slots.address = "1 Test St"
    state.slots.contact_email = "d@example.com"
    record = await create_quote_request(
        state, thread_id="t", measurements={"note": "from context", "floorAreaSquareFeet": 508},
        quote_draft=None,
    )
    assert record.roomName is None and record.homeId is None
    assert record.measurements["floorAreaSquareFeet"] == 508


def test_an_inferred_room_name_warns_operations(home):
    """Ops should not hear "primary bathroom" as fact when it is a guess."""
    state = _captured()
    state.home_id = "h1"
    state.active_room_key = "room-1"
    index = _cache["h1"]
    index.by_key("room-1").confident = False
    measurements, _, _ = _room_measurements(state)
    assert "inferred from fixtures" in measurements["nameCaveat"]


# --------------------------------------------------- the confirm-button loop
def test_confirm_claims_are_detected():
    from app.flow_runtime import _CLAIMS_CONFIRMED as R

    for message in ("i already confirmed", "I tapped it", "yes i hit confirm",
                    "it is confirmed", "i just confirmed it"):
        assert R.search(message), message
    # Not a claim: asking the agent to confirm something.
    for message in ("can you confirm the color", "what about paint"):
        assert not R.search(message), message


def test_the_agent_explains_the_confirm_control_once():
    state = _captured()
    state.client_flow_aware = True
    text = _directives(state)
    assert "point them to the Confirm button" in text
    assert "Do NOT explain it again" not in text


def test_it_stops_repeating_when_they_say_they_confirmed():
    """One claim is enough. At a threshold of two the nag still landed:
    "If you only confirmed here in chat, use the Confirm button" to a
    homeowner who had just said he did."""
    state = _captured()
    state.client_flow_aware = True
    state.confirm_claims = 1
    text = _directives(state)
    assert "Do NOT explain it again" in text
    assert "do not contradict them" in text
    assert "move the conversation forward" in text


@pytest.mark.parametrize("message", [
    "k confirmed lets go how long till i hear back",
    "i did hit confirm alrdy stop askin lol",
    "already did it",
    "we tapped confirm",
])
def test_confirm_claims_survive_typos_and_shorthand(message):
    """Real users do not write clean sentences -- these exact phrasings
    slipped past the first regex and the nag kept coming."""
    from app.flow_runtime import _CLAIMS_CONFIRMED

    assert _CLAIMS_CONFIRMED.search(message), message
