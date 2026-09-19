"""Three things the Sep 11 client call asked for.

1. Quintin asked the agent what flooring he had and it could not say. His
   home had never been through the appearance pass, and separately nothing
   ever put that pass's output in front of the model, so the agent saw
   geometry with no surfaces on it. Both halves are covered here, including
   the case that matters most: with no appearance data the agent must say so
   rather than guess, which is what the same call agreed builds trust.
2. Room words are regional and the homeowner's word is the right one. The
   zip is already captured, so the vocabulary directive follows from it.
3. The service catalog grew from six trades to thirteen, and detection has
   to keep every pair of them apart.
"""

import pytest

import app.flow_runtime as flow_runtime
from app.flow import regional_naming
from app.flow.state import FlowState, Slots
from app.home_guide_tools import (
    KNOWN_SERVICE_TYPES,
    SCAN_SUPPORT,
    detect_service_type,
    get_service_catalog,
    normalize_service_type,
)
from app.home_index import _SYNONYMS, HomeIndex, Room


def _room(key="room-1", name="kitchen"):
    return Room(
        key=key, index=1, storey=0, plan_label="kitchen", area_sqft=180.0,
        floor_y=0.0, display_name=name, role="kitchen", confident=True,
    )


# ------------------------------------------------- 1. appearance directives
def test_surfaces_from_the_scan_reach_the_model(monkeypatch):
    monkeypatch.setattr(
        flow_runtime, "_home_directives", flow_runtime._home_directives
    )
    from app.flow import home_registry

    monkeypatch.setattr(
        home_registry, "room_context_for",
        lambda home_id, room_key: {
            "room_key": room_key,
            "surfaces": {"floor": "honey-toned hardwood", "walls": "warm taupe"},
            "objects": [
                {"class": "oven", "appearance": "stainless", "certainty": "observed"},
            ],
            "style": "transitional",
            "notable": ["a visible tile transition strip"],
            "coverage": "complete",
        },
    )
    text = "\n".join(flow_runtime._appearance_directives("home-1", _room()))
    assert "honey-toned hardwood" in text
    assert "warm taupe" in text
    assert "stainless" in text
    assert "transitional" in text
    assert "only" in text.lower(), "the model must be told this is the whole list"


def test_no_appearance_data_becomes_an_instruction_to_admit_it(monkeypatch):
    """The failure Quintin actually hit. An agent that cannot see the floor
    must say so, not invent a material."""
    from app.flow import home_registry

    monkeypatch.setattr(home_registry, "room_context_for", lambda *a: None)
    text = " ".join(flow_runtime._appearance_directives("home-1", _room())).lower()
    assert "shape only" in text
    assert "flooring" in text
    assert "never describe a material" in text


def test_unphotographed_fixtures_may_be_named_but_not_described(monkeypatch):
    from app.flow import home_registry

    monkeypatch.setattr(
        home_registry, "room_context_for",
        lambda home_id, room_key: {
            "room_key": room_key,
            "surfaces": {"floor": "tile"},
            "objects": [
                {"class": "sofa", "appearance": "", "certainty": "unobserved"},
                {"class": "table", "appearance": "", "certainty": "unobserved"},
            ],
            "style": "",
            "notable": [],
            "coverage": "partial",
        },
    )
    text = " ".join(flow_runtime._appearance_directives("home-1", _room()))
    assert "sofa" in text and "table" in text
    assert "never describe their colour, material or condition" in text.lower()


def test_an_unenriched_room_never_claims_a_material(monkeypatch):
    """Whole-directive check: no surface word survives into the prompt when
    the pass returned an empty document."""
    from app.flow import home_registry

    monkeypatch.setattr(
        home_registry, "room_context_for",
        lambda home_id, room_key: {
            "room_key": room_key, "surfaces": {}, "objects": [],
            "style": "", "notable": [], "coverage": "geometry_only",
        },
    )
    text = " ".join(flow_runtime._appearance_directives("home-1", _room())).lower()
    assert "nothing usable" in text
    assert "never guess a material" in text


# --------------------------------------------------- 2. regional vocabulary
@pytest.mark.parametrize("zip_code,region", [
    ("37203", "south"),      # Nashville, TakeShape's own market
    ("02139", "northeast"),
    ("60614", "midwest"),
    ("90210", "west"),
    ("73301", "south"),
])
def test_zip_maps_to_a_region(zip_code, region):
    assert regional_naming.region_for_zip(zip_code) == region


@pytest.mark.parametrize("value", [None, "", "372", "M5V 2T6", "abcde", "3720"])
def test_an_unusable_zip_gets_no_regional_guess(value):
    assert regional_naming.region_for_zip(value) is None
    assert regional_naming.directive_for(value) is None


def test_the_vocabulary_directive_defers_to_the_homeowner():
    directive = regional_naming.directive_for("37203")
    assert "the South" in directive
    assert "den" in directive
    lowered = directive.lower()
    assert "never correct their word" in lowered


def test_regional_words_resolve_to_the_right_room():
    """A directive alone is not enough: "the front room" has to actually
    find the living room, or the agent says it has no such space."""
    index = HomeIndex([
        _room("room-1", "living room"),
        _room("room-2", "dining room"),
    ])
    index.rooms[0].role = "living room"
    index.rooms[1].role = "dining room"
    assert _SYNONYMS["front room"] == "living room"
    assert _SYNONYMS["sitting room"] == "living room"
    assert _SYNONYMS["lower level"] == "basement"
    assert _SYNONYMS["breakfast nook"] == "dining room"


def test_local_synonyms_win_over_regional_ones():
    """The local table was tuned against the real houses; a regional entry
    must never quietly redefine one of its answers."""
    assert _SYNONYMS["den"] == "living room"
    assert _SYNONYMS["master bath"] == "primary bathroom"


def test_the_vocabulary_directive_rides_on_the_captured_zip():
    state = FlowState(thread_id="t", slots=Slots(zip="37203"))
    assert regional_naming.directive_for(state.slots.zip) is not None
    assert regional_naming.directive_for(FlowState(thread_id="t").slots.zip) is None


# ------------------------------------------------------- 3. service catalog
def test_quintins_list_is_in_the_catalog():
    for service in [
        "Painting", "Roofing & Siding", "Flooring", "Window & Door Install",
        "Window Cleaning", "Power Washing", "Gutter Cleaning",
        "Interior Cleaning", "Handyman", "Interior Remodeling", "Moving",
        "Junk Removal",
    ]:
        assert service in KNOWN_SERVICE_TYPES


def test_every_service_has_scope_examples_and_a_scan_verdict():
    catalog = get_service_catalog("37203")
    assert len(catalog) == len(KNOWN_SERVICE_TYPES)
    for entry in catalog:
        assert entry["scopeExamples"], entry["serviceType"]
        assert entry["scanSupport"] in {"measured", "partial", "exterior"}
    assert set(SCAN_SUPPORT) == set(KNOWN_SERVICE_TYPES)


def test_the_trades_an_interior_walk_cannot_measure_are_marked():
    """Five of the twelve price off the exterior envelope, which an interior
    LiDAR walk never captures. The lead package must not imply otherwise."""
    for service in ["Roofing & Siding", "Gutter Cleaning", "Power Washing"]:
        assert SCAN_SUPPORT[service] == "exterior"
    assert SCAN_SUPPORT["Painting"] == "measured"


@pytest.mark.parametrize("phrase,expected", [
    # The pairs that a first-match list gets wrong.
    ("clean my windows", "Window Cleaning"),
    ("my gutters need cleaning", "Gutter Cleaning"),
    ("clean the gutters out", "Gutter Cleaning"),
    ("deep clean before move-in", "Interior Cleaning"),
    ("looking for movers next month", "Moving"),
    ("the roof is leaking", "Roofing & Siding"),
    ("siding needs replacing", "Roofing & Siding"),
    ("replace the windows in the den", "Window & Door Install"),
    ("I want a full kitchen remodel", "Interior Remodeling"),
    ("need a handyman for odd jobs", "Handyman"),
    ("haul away the junk in the garage", "Junk Removal"),
    ("maid service every two weeks", "Interior Cleaning"),
    ("pressure wash the driveway", "Power Washing"),
    # And the six that already worked, which must not regress.
    ("kitchen repaint", "Painting"),
    ("new hardwood floors", "Flooring"),
    ("deck railing", "Decking"),
])
def test_detection_keeps_thirteen_trades_apart(phrase, expected):
    assert detect_service_type(phrase) == expected
    assert normalize_service_type(phrase) == expected


@pytest.mark.parametrize("phrase", ["refresh", "something nicer", "", None])
def test_nothing_in_the_catalog_still_means_none(phrase):
    assert normalize_service_type(phrase) is None


# ------------------------------- 4. the appearance survives a wiped disk
def test_appearance_reaches_a_server_that_never_saw_the_export(tmp_path, monkeypatch):
    """The failure this guards against has happened twice in this codebase:
    data that only ever lived on the ingesting machine's disk, so the
    deployed service answered as though the pass had never run. The
    appearance has to ride on the index, which is the object that is
    uploaded, exactly as measurements already do."""
    from app.config import settings
    from app.flow import home_registry

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    room = _room()
    room.appearance = {
        "room_key": "room-1",
        "surfaces": {"floor": "honey-toned hardwood"},
        "objects": [],
        "style": "transitional",
        "notable": [],
        "coverage": "complete",
    }
    home_registry.save_index("home-x", HomeIndex([room], bundle_id="home-x"))
    home_registry._cache.clear()

    # A fresh host: the index is there (it is durable), no room_context
    # document is (the disk was wiped).
    reloaded = home_registry.load_index("home-x")
    assert reloaded.rooms[0].appearance, "appearance must survive the round trip"

    document = home_registry.room_context_for("home-x", "room-1")
    assert document is not None
    assert document["surfaces"]["floor"] == "honey-toned hardwood"

    text = " ".join(flow_runtime._appearance_directives("home-x", reloaded.rooms[0]))
    assert "honey-toned hardwood" in text
