"""Fixes from the Sep 10 branch review (fourteen confirmed findings)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

import app.flow_runtime as flow_runtime
from app.config import settings
from app.flow import home_registry, partners, supabase_store
from app.flow.machine import EXTENSION_GENERIC_ONCE, EXTENSION_NAMED_ROOMS, FlowEngine, TurnPlan, GateDecision
from app.flow.ops_email import provider_row_view, send_ops_email
from app.flow.provider_ranking import RankingWeights, rank_candidates
from app.flow.state import FlowState, ScanProcessingState, ScanStatus, ScopeIntent, Slots
from app.flow_quotes import QuoteRequestRecord, build_model_link
from app.flow_runtime import _apply_capture, _detect_room_naming, _detect_scope_intent, _record_asks_and_wordings
from app.home_index import HomeIndex, Room
from app.main import app


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "scan_complete_signal", "processor_job")
    home_registry._cache.clear()
    yield tmp_path
    home_registry._cache.clear()


def _state(**kw) -> FlowState:
    s = FlowState(thread_id="t-review", opening_delivered=True)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# 1. contact / quote-offer directives regardless of scope
def test_contact_and_quote_offer_directives_appear_when_scope_is_undecided():
    state = _state(slots=Slots(first_name="Dana", project_type="Painting"), user_turns=3)
    plan = FlowEngine().plan_turn(state, "what next?")
    text = flow_runtime._build_directives(state, plan, opening=False, price_guidance=None, quotes_to_present=None)
    assert state.scope_intent is ScopeIntent.UNDECIDED
    assert "ask for an email or phone number" in text
    assert "Mention the option of getting real provider quotes AT MOST ONCE" in text


# 2 + 3. rehydrate merges by key and never pushes sample rows
@pytest.fixture
def durable(monkeypatch):
    store: dict[str, dict] = {}

    async def list_rows():
        return [dict(v) for v in store.values()]

    async def upsert(entries):
        for e in entries:
            store[e["key"]] = e["record"]
        return True

    monkeypatch.setattr(supabase_store, "enabled", lambda: True)
    monkeypatch.setattr(supabase_store, "list_partner_rows", list_rows)
    monkeypatch.setattr(supabase_store, "upsert_partner_rows", upsert)
    return store


@pytest.mark.asyncio
async def test_rehydrate_keeps_the_newer_local_row_and_pushes_it_up(durable, tmp_path):
    durable["nash painting"] = {"name": "Nash Painting", "serviceTypes": ["Painting"], "zips": ["37203"],
                                "relationship": "quoted", "quotedCount": 1, "updatedAt": "2026-09-01T00:00:00+00:00"}
    # A local promotion whose upsert failed: newer stamp, higher count.
    (tmp_path / "partners.json").write_text(json.dumps([
        {"name": "Nash Painting", "serviceTypes": ["Painting"], "zips": ["37203"], "relationship": "quoted",
         "quotedCount": 2, "updatedAt": "2026-09-10T00:00:00+00:00"},
        {"name": "Local Only Co.", "serviceTypes": ["Painting"], "zips": ["37203"], "relationship": "partner"},
        {"name": "SAMPLE ROW — Partner Painting Co.", "serviceTypes": ["Painting"], "zips": ["37203"], "sample": True},
    ]), encoding="utf-8")
    assert await partners.rehydrate() == "rehydrated"
    assert durable["nash painting"]["quotedCount"] == 2, "the newer local copy won and was pushed"
    assert "local only co." in durable, "a real local-only row is pushed, not wiped"
    assert not any("sample" in k for k in durable), "sample rows never go up"
    local = json.loads((tmp_path / "partners.json").read_text(encoding="utf-8"))
    assert {r["name"] for r in local} == {"Nash Painting", "Local Only Co."}


@pytest.mark.asyncio
async def test_rehydrate_prefers_the_newer_durable_row(durable, tmp_path):
    durable["nash painting"] = {"name": "Nash Painting", "serviceTypes": ["Painting"], "zips": ["37203"],
                                "relationship": "quoted", "quotedCount": 3, "updatedAt": "2026-09-10T00:00:00+00:00"}
    (tmp_path / "partners.json").write_text(json.dumps([
        {"name": "Nash Painting", "serviceTypes": ["Painting"], "zips": ["37203"], "quotedCount": 1,
         "updatedAt": "2026-09-01T00:00:00+00:00"}]), encoding="utf-8")
    await partners.rehydrate()
    assert durable["nash painting"]["quotedCount"] == 3
    assert partners.find_prospects("Painting", "37203")[0]["quotedCount"] == 3


def test_a_quote_on_a_fresh_host_does_not_keep_seed_rows_in_the_local_file(tmp_path):
    partners.record_quoted_provider("Nash Painting", "Painting", "37203")
    local = json.loads((tmp_path / "partners.json").read_text(encoding="utf-8"))
    assert [r["name"] for r in local] == ["Nash Painting"]
    assert local[0]["updatedAt"]
    assert all(not e["record"].get("sample") for e in partners._entries(partners._load_rows()))


# 4. device_bake keeps the processor state for the model link
@pytest.mark.asyncio
async def test_device_bake_gate_does_not_fake_a_processor_model(monkeypatch):
    monkeypatch.setattr(settings, "scan_complete_signal", "device_bake")
    monkeypatch.setattr(flow_runtime, "_server_job_state", lambda job_id: ScanProcessingState.PROCESSING)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        resp = await http.post("/api/v1/ai/home-chat", json={
            "threadId": "t-bake", "message": "hi",
            "scanContext": {"jobId": "job-1", "processingState": "processing", "localModelReady": True}})
    body = resp.json()
    assert body["flow"]["gates"]["canPromptAdditionalScan"] is True
    state = await flow_runtime.resolve_flow_state("t-bake", body["flow"]["token"])
    assert state.scan.state is ScanProcessingState.COMPLETE
    assert state.scan.processor_state is ScanProcessingState.PROCESSING
    link = await build_model_link(state)
    assert link["status"] == "not_available" and "processing" in link["reason"]
    journal = json.loads((settings.storage_dir and __import__("pathlib").Path(settings.storage_dir) / "flow_journal" / "journal.jsonl").read_text().splitlines()[-1])
    assert journal["flow"]["scanProcessorState"] == "processing" and journal["flow"]["scanState"] == "complete"


# 5. Places numbers expire at read time
def test_expired_places_numbers_are_not_ranked_or_shown(monkeypatch):
    monkeypatch.setattr(settings, "provider_discovery_ttl_days", 30)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat(timespec="seconds")
    fresh = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
    row = {"name": "Old Numbers Co.", "presences": [
        {"platform": "google", "profileUrl": "https://maps.google.com/?cid=1", "rating": 4.9, "reviewCount": 300,
         "source": "google_places_api", "lastVerifiedAt": old, "placeId": "ChIJa"},
        {"platform": "instagram", "profileUrl": "https://www.instagram.com/x/", "followerCount": 900,
         "source": "ops_entry", "lastVerifiedAt": old}]}
    scrubbed = partners.expire_stale_presences(row)
    google = scrubbed["presences"][0]
    assert google["rating"] is None and google["reviewCount"] is None and google["expired"] is True
    assert google["profileUrl"] and google["placeId"] == "ChIJa", "link and place id are kept"
    assert scrubbed["presences"][1]["followerCount"] == 900, "only expiring sources expire"
    assert row["presences"][0]["rating"] == 4.9, "the stored row is untouched"
    assert not partners.presence_expired({"source": "google_places_api", "lastVerifiedAt": fresh})
    assert partners.presence_expired({"source": "google_places_api"}), "no timestamp is stale"
    entry = rank_candidates([scrubbed])[0]
    assert entry["components"]["platforms"]["google"]["score"] is None
    assert provider_row_view(entry)["presences"][0]["metric"] is None


# 6. scope backstop: negation and questions
@pytest.mark.parametrize("phrase,expected", [
    ("just this room, not the whole house", ScopeIntent.SINGLE_ROOM),
    ("not the whole house, a couple of rooms", ScopeIntent.SELECTED_ROOMS),
    ("we're not doing the whole house", None),
    ("does every room need primer?", None),
    ("the whole house, honestly", ScopeIntent.WHOLE_HOME),
])
def test_scope_backstop_handles_negation_and_questions(phrase, expected):
    assert _detect_scope_intent(phrase) is expected


# 7. naming never triggers on a question
def test_questions_do_not_rename_rooms():
    index = HomeIndex([Room(key="room-1", index=1, storey=1, plan_label="unidentified", area_sqft=40.0, floor_y=0.0,
                            display_name="unnamed area 1", confident=False)], bundle_id="B", storey_count=1)
    state = _state(home_id="h", active_room_key="room-1")
    for q in ("which one is the office", "Which room is the garage?", "is that the kitchen", "what is the pantry"):
        assert _detect_room_naming(state, index, q) is None, q
    assert _detect_room_naming(state, index, "the small room is the mudroom.") == ("room-1", "mudroom")


# 8. a named-room invitation does not spend the generic offer
def test_named_room_invitation_keeps_the_generic_offer():
    state = _state(scope_intent=ScopeIntent.SELECTED_ROOMS, scope_rooms=["kitchen", "bathroom"],
                   scan=ScanStatus(state=ScanProcessingState.COMPLETE))
    plan = FlowEngine().plan_turn(state, "ok")
    assert plan.gates.extension_prompt_mode == EXTENSION_NAMED_ROOMS
    _record_asks_and_wordings(state, plan, "You could capture the bathroom next, walking from the kitchen.", opening=False)
    assert state.extension_offers == 0
    _record_asks_and_wordings(state, plan, "Is there anything else you'd want to include?", opening=False)
    assert state.extension_offers == 1
    single = _state(scope_intent=ScopeIntent.SINGLE_ROOM, scan=ScanStatus(state=ScanProcessingState.COMPLETE))
    plan = FlowEngine().plan_turn(single, "ok")
    assert plan.gates.extension_prompt_mode == EXTENSION_GENERIC_ONCE
    _record_asks_and_wordings(single, plan, "You could capture another room if you like.", opening=False)
    assert single.extension_offers == 1, "under single_room any invitation is the one offer"


# 9. the cap never drops a partner or a past quoter
def test_rank_cap_keeps_partners_and_quoted_rows(tmp_path):
    rows = [{"name": f"Discovered {i}", "serviceTypes": ["Painting"], "zips": ["37203"], "relationship": "prospect",
             "source": "google_places", "presences": [{"platform": "google", "rating": 4.5, "reviewCount": 50 + i,
                                                       "source": "google_places_api",
                                                       "lastVerifiedAt": datetime.now(timezone.utc).isoformat()}]}
            for i in range(10)]
    rows.append({"name": "Quiet Partner Co.", "serviceTypes": ["Painting"], "zips": ["37203"]})
    rows.append({"name": "Quiet Quoter", "serviceTypes": ["Painting"], "zips": ["37203"], "relationship": "quoted",
                 "source": "returned_quote", "quotedCount": 1})
    (tmp_path / "partners.json").write_text(json.dumps(rows), encoding="utf-8")
    ranked = partners.rank_for_lead("Painting", "37203", limit=8)
    names = [e["name"] for e in ranked]
    assert "Quiet Partner Co." in names and "Quiet Quoter" in names
    assert len(ranked) == 10 and names[:8] == [e["name"] for e in ranked[:8]]


# 10. PAST QUOTER from the fact, not the weight
def test_past_quoter_mark_survives_a_zero_boost_weight():
    entry = rank_candidates([{"name": "Q", "relationship": "quoted", "quotedCount": 3}], RankingWeights(quoted_boost=0.0))[0]
    assert entry["components"]["quotedBoost"] == 0.0
    view = provider_row_view(entry)
    assert view["pastQuoter"] is True and view["quotedCount"] == 3


# 11. the deprecation warning fires on the real email path
@pytest.mark.asyncio
async def test_legacy_flag_warns_from_send_ops_email(monkeypatch, caplog):
    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    monkeypatch.setattr(settings, "preferred_partner_ordering_enabled", True)
    partners._legacy_warned = False
    record = QuoteRequestRecord(id="qr_legacy", createdAt="2026-09-10T00:00:00+00:00", threadId="t", status="submitted",
                                serviceType="Painting", zip="37203", firstName="Dana", synopsis="paint")
    assert await send_ops_email(record) == "outbox"
    assert any("deprecated" in r.message for r in caplog.records)


# 12. one home id per home in the listing
@pytest.mark.asyncio
async def test_home_listing_dedupes_sanitised_ids(monkeypatch, tmp_path):
    index = HomeIndex([Room(key="room-1", index=1, storey=1, plan_label="kitchen", area_sqft=100.0, floor_y=0.0,
                            display_name="kitchen", confident=True)], bundle_id="B", storey_count=1)
    home_registry.save_index("Smith House", index)

    async def durable_ids():
        return ["Smith House"]

    monkeypatch.setattr(supabase_store, "list_home_indexes", durable_ids)
    assert await home_registry.list_home_ids_async() == ["Smith House"]


# 13. "this room" resolves to the active room when an index is loaded
def test_deictic_scope_rooms_resolve_to_the_active_room():
    index = HomeIndex([Room(key="room-4", index=4, storey=1, plan_label="kitchen", area_sqft=356.0, floor_y=0.0,
                            display_name="kitchen", confident=True)], bundle_id="B", storey_count=1)
    state = _state(home_id="h", active_room_key="room-4")
    _apply_capture(state, {"scopeIntent": "selected_rooms", "scopeRooms": ["this room", "bathroom"]},
                   "this one and the bathroom", home_index=index)
    assert state.scope_rooms == ["kitchen", "bathroom"]
    bare = _state()
    _apply_capture(bare, {"scopeIntent": "selected_rooms", "scopeRooms": ["this room", "bathroom"]}, "x")
    assert bare.scope_rooms == ["bathroom"], "nothing to resolve to: the deictic phrase is dropped, not stored"
