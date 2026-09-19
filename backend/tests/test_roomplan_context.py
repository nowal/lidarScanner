"""Tests for the RoomPlan → context-packet converter, against the real
CapturedRoom fixture vendored from takeshape-mobile."""

from pathlib import Path

import pytest

from app.home_ai import HomeAIContextPacket
from scripts.roomplan_context import context_from_captured_room

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "takeshape-mobile"
    / "LidarAITests"
    / "captured-room.json"
)


@pytest.mark.skipif(not FIXTURE.exists(), reason="takeshape-mobile fixture not present")
def test_real_captured_room_converts_to_valid_context():
    context = context_from_captured_room(FIXTURE)
    # Must validate against the actual wire model the chat endpoint accepts.
    packet = HomeAIContextPacket.model_validate(context)
    room = packet.rooms[0]
    assert packet.roomCount == 1
    # Known ground truth for this fixture (8.82m x 5.97m living space).
    assert room["boundingWidthMeters"] == pytest.approx(8.82, abs=0.01)
    assert room["boundingLengthMeters"] == pytest.approx(5.97, abs=0.01)
    # True polygon footprint (shoelace over floors[].polygonCorners), not the
    # bbox rectangle: 47.2 m² vs 52.7 m² — the bbox overestimates ~10%.
    assert room["floorAreaSquareMeters"] == pytest.approx(47.21, abs=0.1)
    assert room["floorAreaIsTrueFootprint"] is True
    assert room["wallCount"] == 10
    assert room["windowCount"] == 2
    assert room["doorCount"] == 1
    assert room["objectCount"] == 9
    assert room["story"] == 0
    assert len(room["windowSizes"]) == 2
    assert room["windowSizes"][0]["widthMeters"] == pytest.approx(1.67, abs=0.01)
    categories = {obj["category"] for obj in room["objects"]}
    assert {"sofa", "chair", "table", "storage"} <= categories
    assert all("confidence" in obj for obj in room["objects"])
    assert "508" in packet.floorplanSummary  # 47.21 m² → ~508 sq ft
    assert packet.selectedKeyframes == []
