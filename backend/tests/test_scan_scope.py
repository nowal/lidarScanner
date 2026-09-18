"""Scope intent (docs/SCAN_SCOPE.md): one room, a few named rooms, or the
whole home -- captured from the homeowner's words, shaping step-6 extension
prompts, carried into the lead package, surviving restarts.

The SOW §3 hard constraint is tested as a matrix: every scope value crossed
with every processing state, under BOTH flag sources
(LIDARAI_SCAN_COMPLETE_SIGNAL = processor_job | device_bake). A violation
fails loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow import enforcement, supabase_store
from app.flow.machine import (
    EXTENSION_CLOSED,
    EXTENSION_GENERIC_ONCE,
    EXTENSION_NAMED_ROOMS,
    EXTENSION_NONE,
    EXTENSION_OPEN,
    FlowEngine,
    GateDecision,
    extension_prompt_mode,
)
from app.flow.ops_email import build_ops_email, build_ops_email_html
from app.flow.state import FlowState, ScanProcessingState, ScanStatus, ScopeIntent, Slots
from app.flow.tokens import FlowTokenCodec
from app.flow.wording import SCOPE_WORDINGS, assign_scope_wording, wording_by_id
from app.flow_quotes import QuoteRequestRecord, scope_label
from app.flow_runtime import _apply_capture, _detect_scope_intent, encode_flow_token
from app.home_ai import HomeAIChatMessage, HomeAIChatResponse, HomeAIConversationState
from app.main import app

SCOPES = list(ScopeIntent)
NOT_COMPLETE = [s for s in ScanProcessingState if s is not ScanProcessingState.COMPLETE]
SIGNALS = ("processor_job", "device_bake")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "scan_complete_signal", "processor_job")
    yield tmp_path


def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def make_state(**kwargs) -> FlowState:
    state = FlowState(thread_id="t-scope", opening_delivered=True)
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def _stub(content: str, capture: dict | None = None):
    async def _generate(request, flow_directives=None, max_images_override=None):
        response = HomeAIChatResponse(
            threadId=request.threadId,
            message=HomeAIChatMessage(role="assistant", content=content),
            state=HomeAIConversationState(intent="exploring"),
            model="stub",
            provider="stub",
        )
        response._raw_message = content
        if capture is not None:
            response._flow_capture = capture
        return response

    return _generate


# ------------------------------------------------- 1. the field and its default
def test_scope_defaults_to_undecided_and_rides_the_token_and_the_view():
    state = FlowState(thread_id="t")
    assert state.scope_intent is ScopeIntent.UNDECIDED and state.scope_rooms == []
    view = state.client_view()["slots"]
    assert view["scopeIntent"] == "undecided" and view["scopeRooms"] == []

    state.scope_intent = ScopeIntent.SELECTED_ROOMS
    state.scope_rooms = ["kitchen", "primary bath"]
    state.scope_asks = 1
    state.extension_offers = 1
    codec = FlowTokenCodec("secret")
    decoded = codec.decode(codec.encode(state))
    assert decoded.scope_intent is ScopeIntent.SELECTED_ROOMS
    assert decoded.scope_rooms == ["kitchen", "primary bath"]
    assert decoded.scope_asks == 1 and decoded.extension_offers == 1


# ------------------------------------------------- 2. captured, never inferred
def test_scope_comes_from_the_model_capture_or_the_homeowners_own_phrase():
    state = make_state()
    delta = _apply_capture(state, {"scopeIntent": "whole_home"}, "we want to redo everything")
    assert state.scope_intent is ScopeIntent.WHOLE_HOME and delta["scopeIntent"] == "whole_home"

    state = make_state()
    delta = _apply_capture(state, {"scopeIntent": None}, "Honestly it's just this room for now.")
    assert state.scope_intent is ScopeIntent.SINGLE_ROOM and delta["scopeIntent"] == "single_room"

    state = make_state()
    delta = _apply_capture(state, {"scopeIntent": "selected_rooms", "scopeRooms": ["kitchen", "the kitchen", "hall bath"]},
                           "the kitchen and the hall bath")
    assert state.scope_intent is ScopeIntent.SELECTED_ROOMS
    assert state.scope_rooms == ["kitchen", "hall bath"], "near-duplicates collapse"
    assert delta["scopeRooms"] == ["kitchen", "hall bath"]

    # A later explicit statement replaces the earlier one; rooms clear on whole_home.
    _apply_capture(state, {"scopeIntent": "whole_home"}, "actually let's do the whole house")
    assert state.scope_intent is ScopeIntent.WHOLE_HOME and state.scope_rooms == []


def test_scope_is_never_inferred_from_the_scan_or_from_silence():
    state = make_state(home_id="h-19-rooms", active_room_key="room-3")
    _apply_capture(state, {"scopeIntent": None, "scopeRooms": []}, "I like warm neutrals in here")
    assert state.scope_intent is ScopeIntent.UNDECIDED
    # Rooms named without a stated intent do not create one.
    _apply_capture(state, {"scopeIntent": None, "scopeRooms": ["kitchen"]}, "the kitchen is nice too")
    assert state.scope_intent is ScopeIntent.UNDECIDED and state.scope_rooms == []
    # An out-of-vocabulary value is ignored, not coerced.
    _apply_capture(state, {"scopeIntent": "some_rooms"}, "hmm")
    assert state.scope_intent is ScopeIntent.UNDECIDED


@pytest.mark.parametrize("phrase,expected", [
    ("Just this room, honestly.", ScopeIntent.SINGLE_ROOM),
    ("This room is the whole project.", ScopeIntent.SINGLE_ROOM),
    ("We're doing the whole house.", ScopeIntent.WHOLE_HOME),
    ("Every room needs it.", ScopeIntent.WHOLE_HOME),
    ("A couple of rooms, this one and the kitchen.", ScopeIntent.SELECTED_ROOMS),
    ("I'd like warm neutrals please.", None),
    ("The room has three windows.", None),
])
def test_the_phrase_backstop_is_conservative(phrase, expected):
    assert _detect_scope_intent(phrase) is expected


# ------------------------------------------------- 3. asked naturally in step 3
def test_scope_ask_gating_and_wording_variants():
    engine = FlowEngine()
    # Not before a first name / a substantive turn.
    state = make_state(scope_wording_id=SCOPE_WORDINGS[0].id)
    assert engine.evaluate_gates(state, "hi").can_ask_scope is False
    state.slots.first_name = "Dana"
    state.user_turns = 1
    g = engine.evaluate_gates(state, "hi")
    assert g.can_ask_scope is True and engine.plan_turn(state, "hi").scope_wording_id == SCOPE_WORDINGS[0].id
    # Never on a turn that carries the zip ask (one question per reply).
    state.user_turns = 2
    g = engine.evaluate_gates(state, "hi")
    assert g.can_ask_zip is True and g.can_ask_scope is False
    assert g.reasons["can_ask_scope"] == "zip ask takes this turn"
    state.slots.zip = "37203"
    assert engine.evaluate_gates(state, "hi").can_ask_scope is True
    # Placement variant: the after-first-idea wording waits for turn 2.
    late = make_state(scope_wording_id="step3.scope.after_first_idea_v1")
    late.slots.first_name = "Dana"
    late.slots.zip = "37203"
    late.user_turns = 1
    assert engine.evaluate_gates(late, "hi").can_ask_scope is False
    late.user_turns = 2
    assert engine.evaluate_gates(late, "hi").can_ask_scope is True
    # Budget and cooldown.
    engine.record_asks(state, asked_scope=True)
    assert engine.evaluate_gates(state, "hi").can_ask_scope is False   # cooldown
    state.user_turns = 4
    assert engine.evaluate_gates(state, "hi").can_ask_scope is True
    engine.record_asks(state, asked_scope=True)
    state.user_turns = 6
    assert engine.evaluate_gates(state, "hi").can_ask_scope is False
    assert engine.evaluate_gates(state, "hi").reasons["can_ask_scope"] == "scope ask budget spent"
    # Once known, never asked.
    state.scope_asks = 0
    state.scope_intent = ScopeIntent.WHOLE_HOME
    assert engine.evaluate_gates(state, "hi").can_ask_scope is False
    # Sticky variant assignment, every id resolvable.
    assert assign_scope_wording("thread-a") is assign_scope_wording("thread-a")
    assert {assign_scope_wording(f"t{i}").id for i in range(60)} == {w.id for w in SCOPE_WORDINGS}
    assert all(wording_by_id(w.id) is w for w in SCOPE_WORDINGS)
    assert all(w.step == 3 for w in SCOPE_WORDINGS)


@pytest.mark.asyncio
async def test_the_scope_ask_is_journaled_with_its_wording_id(monkeypatch):
    monkeypatch.setattr(flow_runtime, "generate_home_ai_response", _stub(
        "Warm neutrals would suit this room. Is this room the whole project, or are other rooms in the mix too?",
        capture={"firstName": None, "scopeIntent": None, "scopeRooms": []},
    ))
    state = make_state(slots=Slots(first_name="Dana", zip="37203"), user_turns=1, scope_wording_id=None)
    await flow_runtime.persist_flow_state(state)
    async with client() as http:
        resp = await http.post("/api/v1/ai/home-chat", json={
            "threadId": "t-scope", "flowToken": encode_flow_token(state), "message": "I like warm neutrals in here"})
    body = resp.json()
    assert body["flow"]["wordingId"] in {w.id for w in SCOPE_WORDINGS}
    journal = [json.loads(l) for l in (Path(settings.storage_dir) / "flow_journal" / "journal.jsonl").read_text().splitlines()]
    last = journal[-1]
    assert last["wordingIds"] == [body["flow"]["wordingId"]]
    assert last["flow"]["scopeWordingId"] == body["flow"]["wordingId"]
    assert last["flow"]["scopeIntent"] == "undecided"


# ------------------------------------------------- 4. behaviour by scope
@pytest.mark.parametrize("scope,rooms,offers,expected", [
    (ScopeIntent.UNDECIDED, [], 0, EXTENSION_OPEN),
    (ScopeIntent.WHOLE_HOME, [], 0, EXTENSION_OPEN),
    (ScopeIntent.WHOLE_HOME, [], 5, EXTENSION_OPEN),
    (ScopeIntent.SINGLE_ROOM, [], 0, EXTENSION_GENERIC_ONCE),
    (ScopeIntent.SINGLE_ROOM, [], 1, EXTENSION_NONE),
    (ScopeIntent.SELECTED_ROOMS, ["kitchen", "bath"], 0, EXTENSION_NAMED_ROOMS),
    (ScopeIntent.SELECTED_ROOMS, ["kitchen", "bath"], 1, EXTENSION_NAMED_ROOMS),
    (ScopeIntent.SELECTED_ROOMS, [], 0, EXTENSION_GENERIC_ONCE),
    (ScopeIntent.SELECTED_ROOMS, [], 1, EXTENSION_NONE),
])
def test_extension_prompt_mode_per_scope(scope, rooms, offers, expected):
    state = make_state(scope_intent=scope, scope_rooms=rooms, extension_offers=offers,
                       scan=ScanStatus(state=ScanProcessingState.COMPLETE))
    assert extension_prompt_mode(state, True) == expected
    g = FlowEngine().evaluate_gates(state, "hi")
    assert g.extension_prompt_mode == expected and g.client_view()["extensionPromptMode"] == expected


def _gates(mode: str, rooms: list[str] = (), offer: bool = True) -> GateDecision:
    return GateDecision(scan_processing_complete=True, can_prompt_additional_scan=True,
                        extension_prompt_mode=mode, extension_generic_offer_available=offer,
                        scope_rooms=list(rooms), can_ask_zip=True, can_ask_address=True,
                        can_state_prices=True)


def test_enforcement_limits_invitations_to_the_stated_scope():
    rules = lambda text, gates: [v.rule for v in enforcement.check(text, gates)]  # noqa: E731
    # Whole home / undecided: an invitation is fine.
    assert rules("You could capture the rest of the house next.", _gates(EXTENSION_OPEN)) == []
    # Single room, offer available: the generic offer passes, wider does not.
    assert rules("Is there anything else you'd want to include?", _gates(EXTENSION_GENERIC_ONCE)) == []
    assert rules("You could scan the rest of the house too.", _gates(EXTENSION_GENERIC_ONCE)) == ["scan_suggestion_outside_scope"]
    assert rules("Want to capture another room?", _gates(EXTENSION_GENERIC_ONCE)) == ["scan_suggestion_outside_scope"]
    # Describing the scope is not inviting more capture.
    assert rules("Since we're keeping to this room rather than the whole house, warm whites will read well.",
                 _gates(EXTENSION_GENERIC_ONCE)) == []
    assert rules("Good to see your whole home mapped out.", _gates(EXTENSION_NONE, offer=False)) == []
    # Offer spent: even the generic offer is out.
    assert rules("Anything else you'd like to add?", _gates(EXTENSION_NONE, offer=False)) == ["scan_suggestion_outside_scope"]
    assert rules("Let's keep going with the paint colours.", _gates(EXTENSION_NONE, offer=False)) == []
    # Selected rooms: the named room may be invited, others may not.
    named = _gates(EXTENSION_NAMED_ROOMS, rooms=["kitchen", "hall bath"])
    assert rules("If you'd like, you could capture the kitchen next, walking from this room.", named) == []
    assert rules("You could capture the garage as well.", named) == ["scan_suggestion_outside_scope"]
    assert rules("Maybe scan the whole home while you're at it.", named) == ["scan_suggestion_outside_scope"]
    assert enforcement.correction_instruction(
        [enforcement.Violation("scan_suggestion_outside_scope", "x")]).startswith("This homeowner's project is limited")


@pytest.mark.asyncio
async def test_an_invitation_spends_the_single_room_budget_and_the_next_one_is_stripped(monkeypatch):
    state = make_state(scope_intent=ScopeIntent.SINGLE_ROOM,
                       slots=Slots(first_name="Dana", zip="37203", project_type="Painting"),
                       user_turns=3, scan=ScanStatus(state=ScanProcessingState.COMPLETE, verified_complete=True))
    await flow_runtime.persist_flow_state(state)
    monkeypatch.setattr(flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.COMPLETE)
    monkeypatch.setattr(flow_runtime, "generate_home_ai_response",
                        _stub("Lovely. Is there anything else you'd want to include, or shall we stay here?"))
    async with client() as http:
        first = await http.post("/api/v1/ai/home-chat", json={
            "threadId": "t-scope", "flowToken": encode_flow_token(state), "message": "looks good",
            "scanContext": {"jobId": "job-1", "processingState": "complete"}})
        body = first.json()
        assert "anything else" in body["message"]["content"]
        assert body["flow"]["gates"]["extensionPromptMode"] == EXTENSION_NONE, "the one offer is spent"
        assert "step6.extension.offer_v1" in json.loads(
            (Path(settings.storage_dir) / "flow_journal" / "journal.jsonl").read_text().splitlines()[-1])["wordingIds"]

        monkeypatch.setattr(flow_runtime, "generate_home_ai_response",
                            _stub("Great — you could also scan the rest of the house so I can see it all."))
        second = await http.post("/api/v1/ai/home-chat", json={
            "threadId": "t-scope", "flowToken": body["flow"]["token"], "message": "no, just here",
            "scanContext": {"jobId": "job-1", "processingState": "complete"}})
        text = second.json()["message"]["content"]
        assert "rest of the house" not in text and "scan" not in text.lower()
        last = json.loads((Path(settings.storage_dir) / "flow_journal" / "journal.jsonl").read_text().splitlines()[-1])
        assert last["suppressedDrafts"][0]["violations"] == ["scan_suggestion_outside_scope"]


@pytest.mark.asyncio
async def test_a_description_of_the_whole_home_does_not_spend_the_offer_budget(monkeypatch):
    """Opening under an undecided scope, gate open, the reply says 'your
    whole home mapped out'. Then the homeowner narrows to one room: the
    single generic offer must still be available."""
    state = make_state(slots=Slots(first_name="Dana"), user_turns=1,
                       scan=ScanStatus(state=ScanProcessingState.COMPLETE, verified_complete=True))
    await flow_runtime.persist_flow_state(state)
    monkeypatch.setattr(flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.COMPLETE)
    monkeypatch.setattr(flow_runtime, "generate_home_ai_response",
                        _stub("Good to see your whole home mapped out, eight spaces in all. Which space first?"))
    async with client() as http:
        first = await http.post("/api/v1/ai/home-chat", json={
            "threadId": "t-scope", "flowToken": encode_flow_token(state), "message": "hi",
            "scanContext": {"jobId": "job-1", "processingState": "complete"}})
        body = first.json()
        assert body["flow"]["gates"]["extensionPromptMode"] == EXTENSION_OPEN
        monkeypatch.setattr(flow_runtime, "generate_home_ai_response",
                            _stub("Kitchen it is.", capture={"scopeIntent": "single_room", "scopeRooms": ["kitchen"]}))
        second = await http.post("/api/v1/ai/home-chat", json={
            "threadId": "t-scope", "flowToken": body["flow"]["token"], "message": "just the kitchen, this one room",
            "scanContext": {"jobId": "job-1", "processingState": "complete"}})
        assert second.json()["flow"]["gates"]["extensionPromptMode"] == EXTENSION_GENERIC_ONCE, \
            "the description did not spend the offer"


# ------------------------------------------------- 5. the hard constraint matrix
@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("scan_state", NOT_COMPLETE)
def test_no_scan_prompt_before_the_flag_for_every_scope(scope, scan_state):
    state = make_state(scope_intent=scope, scope_rooms=["kitchen"] if scope is ScopeIntent.SELECTED_ROOMS else [],
                       scan=ScanStatus(state=scan_state))
    g = FlowEngine().evaluate_gates(state, "should I scan more?")
    assert g.can_prompt_additional_scan is False, f"VIOLATION: gate open for {scope} at {scan_state}"
    assert g.extension_prompt_mode == EXTENSION_CLOSED
    assert enforcement.check("You could capture the kitchen next.", g)[0].rule == "scan_suggestion_while_processing"


@pytest.mark.parametrize("signal", SIGNALS)
@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.asyncio
async def test_hard_constraint_holds_under_each_flag_source(monkeypatch, signal, scope):
    """For each signal: the OTHER signal's 'complete' must not open the gate,
    and a violating draft is replaced with safe copy; the relevant flag does
    open it (scope then narrows, never widens)."""
    monkeypatch.setattr(settings, "scan_complete_signal", signal)
    monkeypatch.setattr(flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.COMPLETE)
    monkeypatch.setattr(flow_runtime, "generate_home_ai_response",
                        _stub("You should scan another room so I can see more of the house."))
    rooms = ["kitchen"] if scope is ScopeIntent.SELECTED_ROOMS else []
    thread = f"t-{signal}-{scope.value}"

    # The wrong flag: processor says complete under device_bake, and the bake
    # says ready under processor_job (with the processor still running).
    if signal == "device_bake":
        wrong = {"jobId": "job-1", "processingState": "complete"}
        right = {"jobId": "job-1", "processingState": "processing", "localModelReady": True}
    else:
        monkeypatch.setattr(flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.PROCESSING)
        wrong = {"jobId": "job-1", "processingState": "processing", "localModelReady": True}
        right = None
    state = make_state(thread_id=thread, scope_intent=scope, scope_rooms=rooms)
    await flow_runtime.persist_flow_state(state)
    async with client() as http:
        resp = await http.post("/api/v1/ai/home-chat", json={
            "threadId": thread, "flowToken": encode_flow_token(state),
            "message": "should I scan the rest?", "scanContext": wrong})
        body = resp.json()
        assert body["flow"]["gates"]["canPromptAdditionalScan"] is False, \
            f"VIOLATION: {signal} gate opened on the other signal's flag for {scope}"
        assert body["flow"]["gates"]["extensionPromptMode"] == EXTENSION_CLOSED
        assert "scan" not in body["message"]["content"].lower()
        last = json.loads((Path(settings.storage_dir) / "flow_journal" / "journal.jsonl").read_text().splitlines()[-1])
        assert last["suppressedDrafts"][0]["violations"] == ["scan_suggestion_while_processing"]
        assert last["flow"]["scanSignal"] == signal
        assert signal in last["gateReasons"]["can_prompt_additional_scan"]

        if right is not None:
            resp = await http.post("/api/v1/ai/home-chat", json={
                "threadId": thread, "flowToken": body["flow"]["token"],
                "message": "ok now?", "scanContext": right})
            gates = resp.json()["flow"]["gates"]
            assert gates["canPromptAdditionalScan"] is True
            assert gates["extensionPromptMode"] == extension_prompt_mode(
                make_state(scope_intent=scope, scope_rooms=rooms), True)


@pytest.mark.asyncio
async def test_processor_job_signal_opens_on_the_processor_and_ignores_the_bake(monkeypatch):
    monkeypatch.setattr(settings, "scan_complete_signal", "processor_job")
    monkeypatch.setattr(flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.COMPLETE)
    async with client() as http:
        resp = await http.post("/api/v1/ai/home-chat", json={
            "message": "hello", "scanContext": {"jobId": "job-1", "processingState": "processing", "localModelReady": False}})
        assert resp.json()["flow"]["gates"]["canPromptAdditionalScan"] is True, "server-verified processor job wins"


@pytest.mark.asyncio
async def test_device_bake_signal_holds_until_the_bake_reports_and_closes_on_a_rewalk(monkeypatch):
    monkeypatch.setattr(settings, "scan_complete_signal", "device_bake")
    monkeypatch.setattr(flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.COMPLETE)
    async with client() as http:
        resp = await http.post("/api/v1/ai/home-chat", json={"threadId": "t-bake", "message": "hello",
                                                             "scanContext": {"jobId": "job-1", "processingState": "complete"}})
        body = resp.json()
        assert body["flow"]["gates"]["canPromptAdditionalScan"] is False, "no localModelReady: gate stays shut"
        resp = await http.post("/api/v1/ai/home-chat", json={"threadId": "t-bake", "flowToken": body["flow"]["token"],
                                                             "message": "and now?", "scanContext": {"localModelReady": True}})
        body = resp.json()
        assert body["flow"]["gates"]["canPromptAdditionalScan"] is True
        # An update-scan re-walk: the bake is running again.
        resp = await http.post("/api/v1/ai/home-chat", json={"threadId": "t-bake", "flowToken": body["flow"]["token"],
                                                             "message": "added a room",
                                                             "scanContext": {"scanMode": "update_existing", "localModelReady": False}})
        assert resp.json()["flow"]["gates"]["canPromptAdditionalScan"] is False


def test_unknown_signal_setting_falls_back_to_the_processor_job(monkeypatch):
    monkeypatch.setattr(settings, "scan_complete_signal", "something_else")
    assert flow_runtime.scan_complete_signal() == "processor_job"


# ------------------------------------------------- 6. lead package and email
def _record(**overrides) -> QuoteRequestRecord:
    base = dict(id="qr_scope01", createdAt="2026-09-10T12:00:00+00:00", threadId="t-scope", status="submitted",
                serviceType="Painting", zip="37203", firstName="Dana", contactEmail="dana@example.com",
                address="9 Secret Lane, Nashville TN", synopsis="Dana wants paint.", roomName="kitchen")
    base.update(overrides)
    return QuoteRequestRecord(**base)


def test_scope_label_and_ops_view_carry_scope():
    assert scope_label(_record(scopeIntent="whole_home")) == "whole home"
    assert scope_label(_record(scopeIntent="single_room")) == "one room (kitchen)"
    assert scope_label(_record(scopeIntent="single_room", roomName=None)) == "one room"
    assert scope_label(_record(scopeIntent="selected_rooms", scopeRooms=["kitchen", "hall bath"])) == "selected rooms: kitchen, hall bath"
    assert scope_label(_record(scopeIntent="selected_rooms")) == "selected rooms: kitchen"
    assert scope_label(_record()) == "not stated by the homeowner (conversation was about the kitchen)"
    view = _record(scopeIntent="selected_rooms", scopeRooms=["kitchen", "hall bath"]).ops_view()
    assert view["project"]["scope"] == {"intent": "selected_rooms", "rooms": ["kitchen", "hall bath"],
                                        "label": "selected rooms: kitchen, hall bath"}
    assert view["project"]["serviceType"] == "Painting", "trade category is unchanged by scope"
    assert view["address"] is None and "threadId" not in json.dumps(view)


def test_the_email_says_one_room_or_whole_home_and_still_withholds_the_address():
    for intent, rooms, expect_subject, expect_line in [
        ("whole_home", [], "Painting — whole home in 37203", "Scope of work: whole home"),
        ("selected_rooms", ["kitchen", "hall bath"], "Painting — kitchen + hall bath in 37203", "Scope of work: selected rooms: kitchen, hall bath"),
        ("single_room", ["kitchen"], "Painting — kitchen in 37203", "Scope of work: one room (kitchen)"),
        ("undecided", [], "Painting — kitchen in 37203", "Scope of work: not stated by the homeowner"),
    ]:
        record = _record(scopeIntent=intent, scopeRooms=rooms)
        subject, body = build_ops_email(record, [], None, [])
        html = build_ops_email_html(record, [], None, [])
        assert expect_subject in subject, (intent, subject)
        assert expect_line in body, (intent, body)
        assert "Scope of work" in html and expect_line.split(": ", 1)[1].split(" (")[0] in html
        assert "Secret Lane" not in body and "Secret Lane" not in html and "Secret Lane" not in subject
        assert "withheld until the homeowner selects a quote" in body


@pytest.mark.asyncio
async def test_submission_copies_scope_from_flow_state_into_the_record(monkeypatch):
    monkeypatch.setattr(settings, "ops_token", "ops-secret", raising=False)
    state = make_state(scope_intent=ScopeIntent.SELECTED_ROOMS, scope_rooms=["kitchen", "hall bath"],
                       slots=Slots(first_name="Dana", zip="37203", project_type="Painting",
                                   scope_options=["walls"], address="9 Secret Lane", contact_email="d@example.com"))
    await flow_runtime.persist_flow_state(state)
    async with client() as http:
        submitted = await http.post("/api/v1/ai/quote-requests",
                                    json={"threadId": "t-scope", "flowToken": encode_flow_token(state), "confirm": True})
        assert submitted.status_code == 201
        qr_id = submitted.json()["quoteRequestId"]
        view = (await http.get(f"/api/v1/ops/quote-requests/{qr_id}",
                               headers={"Authorization": "Bearer ops-secret"})).json()
    assert view["project"]["scope"]["intent"] == "selected_rooms"
    assert view["project"]["scope"]["rooms"] == ["kitchen", "hall bath"]
    assert view["address"] is None


# ------------------------------------------------- 7. persistence
@pytest.mark.asyncio
async def test_scope_survives_a_restart_through_the_durable_state(monkeypatch, tmp_path):
    """Token dropped, local disk wiped: the Supabase copy restores scope."""
    state = make_state(scope_intent=ScopeIntent.WHOLE_HOME, scope_asks=1, extension_offers=0,
                       slots=Slots(first_name="Dana"))
    stored = {}

    async def upsert(s):
        stored["state"] = s.model_dump(mode="json")

    async def get(thread_id):
        return FlowState.model_validate(stored["state"]) if "state" in stored else None

    monkeypatch.setattr(supabase_store, "enabled", lambda: True)
    monkeypatch.setattr(supabase_store, "upsert_flow_state", upsert)
    monkeypatch.setattr(supabase_store, "get_flow_state", get)
    await flow_runtime.persist_flow_state(state)
    assert stored["state"]["scope_intent"] == "whole_home"
    for f in (tmp_path / "flow_state").glob("*.json"):
        f.unlink()
    restored = await flow_runtime.resolve_flow_state("t-scope", None)
    assert restored.scope_intent is ScopeIntent.WHOLE_HOME and restored.scope_asks == 1
