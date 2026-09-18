"""Not doubling down when an instruction isn't working.

Two failures from the whole-home battery (Sep 4), same shape:

- A terse homeowner asked about a bedroom that was not in the scan and got
  twelve turns of scan-extension instructions and no design help at all.
- A homeowner said he could not see the Confirm card; the agent insisted it
  was there, said it was "attaching the request again", then told him his
  app was broken and to contact support — none of which it can know.

Both are the agent repeating a move that has already failed instead of
changing course.
"""

import json

import pytest

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow.home_registry import _cache, ingest_bundle
from app.flow.state import FlowState
from app.home_ai import HomeAIChatRequest

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A house with a kitchen and nothing else — so a bedroom really is
    absent, as in the real scan that produced the failure."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    _cache.clear()
    base = tmp_path / "bundle"
    (base / "rooms" / "room-1").mkdir(parents=True)
    (base / "meta.json").write_text("{}", encoding="utf-8")
    (base / "rooms" / "room-1" / "floor.json").write_text(
        json.dumps({"floor": 1, "floorY": 0.0}), encoding="utf-8")
    (base / "rooms" / "room-1" / "room.json").write_text(json.dumps({
        "sections": [{"label": "kitchen"}],
        "floors": [{"polygonCorners": [[-3, 0, -3], [3, 0, -3], [3, 0, 3], [-3, 0, 3]],
                    "transform": list(IDENTITY)}],
        "objects": [{"category": {"sink": {}}}, {"category": {"stove": {}}}],
        "windows": [], "doors": [], "walls": [], "openings": [],
    }), encoding="utf-8")
    yield ingest_bundle(base, "h")
    _cache.clear()


def _directives(state, index=None, message="hi"):
    plan = flow_runtime._engine.plan_turn(state, message)
    return flow_runtime._build_directives(
        state, plan, opening=False, price_guidance=None,
        quotes_to_present=None, home_index=index,
    )


def _ask(state, message, home_id="h"):
    return flow_runtime._reconcile_home(
        state, HomeAIChatRequest(threadId="t", message=message, homeId=home_id)
    )


# ------------------------------------------------- the missing-room dead end
def test_first_mention_explains_how_to_add_the_room(home):
    state = FlowState(thread_id="t")
    _ask(state, "i want to redo the bedroom")
    text = _directives(state, home)
    assert state.unresolved_room_turns == 1
    assert "NOT in the scan" in text
    assert "at most once" in text


def test_it_stops_repeating_and_starts_helping(home):
    """The judge's actual complaint: twelve turns of instructions, no help."""
    state = FlowState(thread_id="t")
    for message in ("the bedroom", "ok how?", "which room is closest?"):
        _ask(state, message)
    assert state.unresolved_room_turns >= 2
    text = _directives(state, home)
    assert "STOP repeating those instructions" in text
    assert "describe the space" in text
    assert "give real design guidance" in text


def test_moving_to_a_real_room_resets_the_dead_end(home):
    state = FlowState(thread_id="t")
    _ask(state, "the bedroom")
    _ask(state, "the bedroom again")
    assert state.unresolved_room_turns == 2
    _ask(state, "fine, the kitchen then")
    assert state.unresolved_room_turns == 0
    assert state.unresolved_room_phrase is None
    assert "STOP repeating" not in _directives(state, home)


def test_a_different_missing_room_starts_its_own_count(home):
    state = FlowState(thread_id="t")
    _ask(state, "the bedroom")
    _ask(state, "what about the garage")
    assert state.unresolved_room_turns == 1


# --------------------------------------------------- claims about their screen
@pytest.mark.parametrize("message", [
    "i dont see no card wat am i missin",
    "still nuthin showin up on my end",
    "I can't find the button",
    "nothing showing here",
    "where do i click confirm",
])
def test_cannot_see_ui_is_detected(message):
    assert flow_runtime._CANNOT_SEE_UI.search(message), message


@pytest.mark.parametrize("message", [
    "i see what you mean",
    "that looks good to me",
    "lets do the kitchen",
])
def test_ordinary_messages_are_not_ui_complaints(message):
    assert not flow_runtime._CANNOT_SEE_UI.search(message), message


def test_the_agent_stops_asserting_what_is_on_their_screen():
    state = FlowState(thread_id="t", client_flow_aware=True)
    state.ui_not_visible_claims = 1
    text = _directives(state)
    assert "NO \nview of their app" in text or "NO view of their app" in text.replace("\n", " ")
    assert "do not insist it is there" in text
    assert "do not diagnose their app" in text.replace("\n", " ")


def test_no_such_directive_before_they_complain():
    state = FlowState(thread_id="t", client_flow_aware=True)
    assert "cannot see something on their screen" not in _directives(state)


@pytest.mark.asyncio
async def test_ui_complaints_are_counted_from_the_message(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    state = FlowState(thread_id="t")
    for message in ("i dont see the card", "still nuthin", "lets do the kitchen"):
        if flow_runtime._CANNOT_SEE_UI.search(message):
            state.ui_not_visible_claims += 1
    assert state.ui_not_visible_claims == 2


# ------------------------------------------------------- proportional brevity
def test_one_word_answers_get_a_tighter_cap():
    """35 words still reads as a lecture to someone typing "queen"."""
    state = FlowState(thread_id="t")
    state.recent_user_words = [1, 2, 1]
    text = _directives(state)
    assert "ONE OR TWO WORDS" in text
    assert "20 words at most" in text
    assert "If you can answer in five words, do" in text


def test_short_but_not_telegraphic_answers_get_the_looser_cap():
    state = FlowState(thread_id="t")
    state.recent_user_words = [6, 5, 7]
    text = _directives(state)
    assert "35 words at most" in text
    assert "ONE OR TWO WORDS" not in text


def test_normal_answers_get_neither():
    state = FlowState(thread_id="t")
    state.recent_user_words = [22, 30, 18]
    text = _directives(state)
    assert "35 words at most" not in text and "ONE OR TWO WORDS" not in text


def test_service_type_falls_back_to_what_the_homeowner_typed():
    """The model summarised "paint, new bed" as "bedroom refresh", which
    maps to no trade and would match no partner."""
    from app.home_guide_tools import detect_service_type, normalize_service_type

    assert normalize_service_type("bedroom refresh") is None
    assert detect_service_type("paint, new bed") == "Painting"
