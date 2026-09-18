"""Frame selection geometry, pinned on synthetic scenes with known answers.

The projection maths is the part that fails silently: a wrong convention or a
transposed rotation still returns plausible-looking frames, just the wrong ones.
So every test here asserts a value that can be worked out by hand.
"""

from __future__ import annotations

import math

import pytest

from app.frame_select import (
    ConventionError,
    camera_pose,
    detect_convention,
    distance_weight,
    frame_score,
    object_boxes,
    score_frame,
    select_frames,
    suppress,
    upright_rotation,
)

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
# Camera rolled 90 degrees about its own view axis: X' = +Y, Y' = -X.
ROLLED_90 = [0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
INTRINSICS = [1000, 0, 0, 0, 1000, 0, 960, 720, 1]


def make_frame(frame_id: str, transform: list[float]) -> dict:
    return {
        "id": frame_id,
        "cameraTransform": list(transform),
        "intrinsics": list(INTRINSICS),
        "imageResolution": [1920, 1440],
        "intrinsicsReferenceResolution": [1920, 1440],
    }


def translated(x: float, y: float, z: float) -> list[float]:
    transform = list(IDENTITY)
    transform[12], transform[13], transform[14] = x, y, z
    return transform


def make_room(*objects: tuple[str, tuple[float, float, float], tuple[float, float, float]]) -> dict:
    """Axis-aligned boxes: (label, centre, dimensions)."""
    entries = []
    for label, centre, dimensions in objects:
        transform = list(IDENTITY)
        transform[12], transform[13], transform[14] = centre
        entries.append(
            {"category": label, "transform": transform, "dimensions": list(dimensions)}
        )
    return {"objects": entries}


# --------------------------------------------------------------------------
# Orientation -- the failure that does not announce itself
# --------------------------------------------------------------------------


def test_upright_rotation_identity_is_zero() -> None:
    """A camera held level with world up in its own +Y needs no correction."""
    assert upright_rotation(IDENTITY) == 0


def test_upright_rotation_detects_a_quarter_turn() -> None:
    assert upright_rotation(ROLLED_90) == 1


def test_upright_rotation_is_stable_across_small_roll() -> None:
    """Real captures wobble. The bundle's own frames sit at roughly -93 degrees
    of roll with a few degrees of variation, and every one must round to the
    same quarter turn or consecutive frames would be rotated differently."""
    turns = set()
    for degrees in (-88.0, -93.0, -98.0, -103.0):
        angle = math.radians(degrees)
        transform = list(IDENTITY)
        # Roll about the view axis: X' and Y' rotate, Z' stays.
        transform[0], transform[1] = math.cos(angle), math.sin(angle)
        transform[4], transform[5] = -math.sin(angle), math.cos(angle)
        turns.add(upright_rotation(transform))
    assert len(turns) == 1


def test_upright_rotation_survives_a_straight_down_camera() -> None:
    """World up projects to nothing when the camera looks at the floor; the
    answer is arbitrary but must not be a crash."""
    looking_down = [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]
    assert upright_rotation(looking_down) in (0, 1, 2, 3)


# --------------------------------------------------------------------------
# Boxes and projection
# --------------------------------------------------------------------------


def test_object_boxes_produces_eight_corners_around_the_centre() -> None:
    room = make_room(("sink", (1.0, 2.0, 3.0), (2.0, 2.0, 2.0)))
    boxes = object_boxes(room)
    assert len(boxes) == 1
    label, corners = boxes[0]
    assert label == "sink"
    assert len(corners) == 8
    assert min(c[0] for c in corners) == pytest.approx(0.0)
    assert max(c[0] for c in corners) == pytest.approx(2.0)
    assert min(c[2] for c in corners) == pytest.approx(2.0)


def test_object_boxes_normalises_the_dict_category_form() -> None:
    """RoomPlan emits "sink" or {"sink": {}} depending on version."""
    room = make_room(("x", (0, 0, 0), (1, 1, 1)))
    room["objects"][0]["category"] = {"bathtub": {}}
    assert object_boxes(room)[0][0] == "bathtub"


def test_object_boxes_skips_malformed_entries() -> None:
    room = {
        "objects": [
            {"category": "sink", "transform": [1, 2, 3], "dimensions": [1, 1, 1]},
            {"category": "sofa", "transform": list(IDENTITY), "dimensions": []},
            {"category": "table", "transform": list(IDENTITY), "dimensions": [1, 1, 1]},
        ]
    }
    assert [label for label, _ in object_boxes(room)] == ["table"]


def test_convention_detection_picks_arkit_negative_z() -> None:
    """ARKit cameras look down -Z. An object placed in front of an identity
    camera is only visible under that convention, so the detector must choose it
    rather than a hardcoded assumption doing so."""
    room = make_room(("sink", (0.0, 0.0, -2.0), (1.0, 1.0, 1.0)))
    frames = [make_frame("a", IDENTITY)]
    assert detect_convention(object_boxes(room), frames) is True


def test_convention_detection_reports_failure_when_nothing_projects() -> None:
    """Geometry and poses in different spaces must be detectable, not guessed
    around -- this is what stops a silently empty selection."""
    room = make_room(("sink", (0.0, 500.0, 0.0), (0.1, 0.1, 0.1)))
    frames = [make_frame("a", IDENTITY)]
    assert detect_convention(object_boxes(room), frames) is None


def test_score_frame_centres_a_box_and_measures_its_depth() -> None:
    room = make_room(("sink", (0.0, 0.0, -2.0), (1.0, 1.0, 0.2)))
    count, area, near, labels = score_frame(
        object_boxes(room), make_frame("a", IDENTITY), negate_z=True
    )
    assert count == 1
    assert labels == ["sink"]
    assert near == pytest.approx(2.0, abs=0.15)
    assert 0.0 < area < 1.0


def test_score_frame_ignores_what_is_behind_the_camera() -> None:
    room = make_room(("sink", (0.0, 0.0, 2.0), (1.0, 1.0, 1.0)))
    count, _area, _near, _labels = score_frame(
        object_boxes(room), make_frame("a", IDENTITY), negate_z=True
    )
    assert count == 0


def test_score_frame_respects_the_intrinsics_reference_resolution() -> None:
    """Intrinsics quoted against a different resolution must be scaled, or the
    principal point lands off-image and everything reads as out of frame."""
    room = make_room(("sink", (0.0, 0.0, -2.0), (0.4, 0.4, 0.4)))
    frame = make_frame("a", IDENTITY)
    frame["imageResolution"] = [960, 720]
    frame["intrinsicsReferenceResolution"] = [1920, 1440]
    count, area, _near, _labels = score_frame(
        object_boxes(room), frame, negate_z=True
    )
    assert count == 1
    assert area > 0


# --------------------------------------------------------------------------
# Scoring shape
# --------------------------------------------------------------------------


def test_distance_weight_peaks_at_the_ideal_standoff() -> None:
    ideal = distance_weight(1.5)
    assert ideal > distance_weight(0.6)   # close-up
    assert ideal > distance_weight(2.8)   # through a doorway
    assert distance_weight(0.0) == 0.0


def test_frame_score_rewards_objects_and_coverage() -> None:
    assert frame_score(6, 0.8, 1.5) > frame_score(2, 0.8, 1.5)
    assert frame_score(6, 0.8, 1.5) > frame_score(6, 0.2, 1.5)


# --------------------------------------------------------------------------
# Suppression and selection
# --------------------------------------------------------------------------


def _row(frame_id: str, score: float, centre, direction) -> dict:
    return {
        "id": frame_id,
        "score": score,
        "centre": centre,
        "direction": direction,
        "objects": 1,
        "area": 0.5,
        "near": 1.5,
        "labels": [],
        "turns": 0,
    }


def test_suppress_drops_near_duplicate_viewpoints() -> None:
    """A scan yields ~10 frames a second, so the best viewpoint wins all of
    them. Without suppression the selection is four pictures of one corner."""
    rows = [
        _row("best", 9.0, (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
        _row("twin", 8.0, (0.1, 0.0, 0.1), (0.0, 0.0, -1.0)),
        _row("other", 7.0, (5.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    ]
    kept = [row["id"] for row in suppress(rows, 3)]
    assert kept == ["best", "other"]


def test_suppress_keeps_the_same_position_seen_from_a_new_angle() -> None:
    rows = [
        _row("north", 9.0, (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
        _row("east", 8.0, (0.2, 0.0, 0.0), (1.0, 0.0, 0.0)),
    ]
    assert len(suppress(rows, 3)) == 2


def test_suppress_breaks_score_ties_deterministically() -> None:
    rows = [
        _row("b", 5.0, (0.0, 0.0, 0.0), (0.0, 0.0, -1.0)),
        _row("a", 5.0, (9.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
    ]
    assert [row["id"] for row in suppress(rows, 1)] == ["a"]


def test_camera_pose_reads_centre_and_view_direction() -> None:
    centre, direction = camera_pose(translated(1.0, 2.0, 3.0))
    assert centre == (1.0, 2.0, 3.0)
    assert direction == (0.0, 0.0, -1.0)  # ARKit looks down -Z


def test_select_frames_is_deterministic_and_bounded() -> None:
    """The appearance pass is cached per room, so a selection that wobbled
    between runs would produce a context document disagreeing with itself."""
    room = make_room(
        ("sink", (0.0, 0.0, -2.0), (0.8, 0.8, 0.4)),
        ("storage", (1.2, 0.0, -2.2), (0.8, 1.6, 0.5)),
    )
    manifest = {
        "frames": [
            make_frame("f1", translated(0.0, 0.0, 0.0)),
            make_frame("f2", translated(0.05, 0.0, 0.05)),
            make_frame("f3", translated(3.0, 0.0, 0.0)),
            make_frame("f4", translated(-3.0, 0.0, 0.0)),
        ]
    }
    first = [f["id"] for f in select_frames(room, manifest, 2)]
    second = [f["id"] for f in select_frames(room, manifest, 2)]
    assert first == second
    assert len(first) <= 2


def test_select_frames_raises_when_geometry_and_poses_disagree() -> None:
    room = make_room(("sink", (0.0, 900.0, 0.0), (0.1, 0.1, 0.1)))
    manifest = {"frames": [make_frame("f1", IDENTITY)]}
    with pytest.raises(ConventionError):
        select_frames(room, manifest, 2)


def test_select_frames_raises_on_a_room_with_no_objects() -> None:
    """room-10 in the real export is a 15 sq ft closet: two walls, no objects,
    three photos. There is nothing to select against and the caller must be able
    to tell that apart from a selection that merely came back small."""
    with pytest.raises(ConventionError):
        select_frames({"objects": []}, {"frames": [make_frame("f1", IDENTITY)]}, 4)


def test_selected_frames_carry_their_upright_correction() -> None:
    room = make_room(("sink", (0.0, 0.0, -2.0), (0.8, 0.8, 0.4)))
    transform = list(ROLLED_90)
    manifest = {"frames": [{**make_frame("f1", transform)}]}
    picks = select_frames(room, manifest, 1)
    assert picks and picks[0]["turns"] == 1
