"""The bundle check script, run on a synthetic export zipped the way a phone
would send it. Real bundles stay off the repository (SOW section 12); this
keeps the script itself honest on every change.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

from app.config import settings
from app.flow import home_registry

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bundle_check  # noqa: E402

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
FLOOR_PLANE = [1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]
INTRINSICS = [1000, 0, 0, 0, 1000, 0, 960, 720, 1]


def _floor(width: float, depth: float, cx: float = 0.0) -> dict:
    hw, hd = width / 2, depth / 2
    t = list(FLOOR_PLANE)
    t[12] = cx
    return {"transform": t,
            "polygonCorners": [[-hw, -hd, 0.0], [hw, -hd, 0.0], [hw, hd, 0.0], [-hw, hd, 0.0]],
            "dimensions": [width, depth, 0.0]}


def _box(label: str, centre, dims=(1.0, 1.0, 1.0)) -> dict:
    t = list(IDENTITY)
    t[12], t[13], t[14] = centre
    return {"category": {label: {}}, "transform": t, "dimensions": list(dims)}


def _camera(x: float, y: float, z: float) -> list[float]:
    t = list(IDENTITY)
    t[12], t[13], t[14] = x, y, z
    return t


def _jpeg() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 6), (120, 110, 100)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _usdz(path: Path, payload: bytes = b"#usda 1.0\n") -> None:
    """The smallest thing that passes for a USDZ: a zip with a USD member."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("model.usda", payload)


def _write_room(base: Path, index: int, *, label: str, width: float, depth: float, cx: float,
                objects: list[dict], walls, frames: list[tuple[str, tuple[float, float, float]]],
                storey: int = 1, model: bool = False) -> None:
    room_dir = base / "rooms" / f"room-{index}"
    (room_dir / "rebuild" / "images").mkdir(parents=True, exist_ok=True)
    if model:
        _usdz(room_dir / "model.usdz")
    (room_dir / "floor.json").write_text(
        json.dumps({"floor": storey, "floorY": 0.0 if storey == 1 else 3.0}), encoding="utf-8")
    (room_dir / "room.json").write_text(json.dumps({
        "sections": [{"label": label}] if label else [],
        "floors": [_floor(width, depth, cx)],
        "objects": objects,
        "walls": [{"dimensions": [w, h, 0.0]} for w, h in walls],
        "doors": [], "windows": [], "openings": [],
    }), encoding="utf-8")
    (room_dir / "rebuild" / "manifest.json").write_text(json.dumps({"frames": [
        {"id": fid, "cameraTransform": _camera(*pos), "intrinsics": list(INTRINSICS),
         "imageResolution": [1920, 1440], "intrinsicsReferenceResolution": [1920, 1440]}
        for fid, pos in frames
    ]}), encoding="utf-8")
    for fid, _ in frames:
        (room_dir / "rebuild" / "images" / f"{fid}.jpg").write_bytes(_jpeg())


@pytest.fixture
def export_zip(tmp_path, monkeypatch) -> Path:
    """Two rooms, zipped with the Scans/<id>/ parents a phone export carries."""
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    home_registry._cache.clear()
    base = tmp_path / "Scans" / "3F2C-TEST"
    (base / "rooms").mkdir(parents=True)
    (base / "meta.json").write_text(json.dumps({"id": "3F2C-TEST"}), encoding="utf-8")
    # Kitchen at the origin: a sink 2 m in front of a camera standing in the room.
    _write_room(base, 1, label="kitchen", width=5.0, depth=4.0, cx=0.0,
                objects=[_box("sink", (0.0, 0.5, -1.8), (0.8, 0.9, 0.6)),
                         _box("stove", (0.6, 0.5, -1.8), (0.6, 0.9, 0.6))],
                walls=((5.0, 2.4), (4.0, 2.4), (5.0, 2.4), (4.0, 2.4)),
                frames=[("k-a", (0.0, 1.4, 0.2)), ("k-b", (0.1, 1.4, 0.3)), ("k-c", (-0.1, 1.4, 0.1))],
                model=True)
    # Primary bathroom far away, walls of storey height (the open-volume warning).
    _write_room(base, 2, label="bathroom", width=3.0, depth=3.0, cx=30.0,
                objects=[_box("bathtub", (30.0, 0.3, -1.0), (1.6, 0.6, 0.7)),
                         _box("sink", (30.8, 0.5, -1.2)), _box("sink", (29.2, 0.5, -1.2)),
                         _box("toilet", (31.0, 0.4, 0.5))],
                walls=((3.0, 5.0), (3.0, 5.0), (3.0, 5.0), (3.0, 5.0)),
                frames=[("b-a", (30.0, 1.4, 0.5))])
    zip_path = tmp_path / "3F2C-TEST.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(base.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(tmp_path))
    yield zip_path
    home_registry._cache.clear()


def _report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_good_export_passes_end_to_end(export_zip, tmp_path, capsys):
    report_path = tmp_path / "report.json"
    code = bundle_check.main([str(export_zip), "--report", str(report_path)])
    out = capsys.readouterr().out
    assert code == 0, out
    report = _report(report_path)
    statuses = {c["check"]: c["status"] for c in report["checks"]}
    assert statuses["layout"] == "PASS"
    assert statuses["resolution"] == "PASS"
    assert statuses["frames"] == "PASS"
    assert statuses["measurements"] == "WARN"          # the 5 m bathroom walls
    assert statuses["models"] == "WARN"                # room-2 was not textured before export
    assert statuses["wire"] == "PASS"
    assert report["meta"]["homeId"] == "3F2C-TEST"      # from meta.json, nested zip root found
    rooms = {r["key"]: r for r in report["rooms"]}
    assert rooms["room-1"]["name"] == "kitchen" and rooms["room-1"]["selectedFrames"]
    assert rooms["room-2"]["name"] == "primary bathroom" or rooms["room-2"]["name"] == "bathroom"
    assert rooms["room-1"]["measurements"]["paintable_sqft"] > 0
    assert rooms["room-1"]["model"]["present"] and rooms["room-1"]["model"]["valid"]
    assert rooms["room-2"]["model"] == {"present": False}
    assert "RESULT: PASS" in out


def test_expectations_are_written_then_diffed(export_zip, tmp_path):
    expect = tmp_path / "expect.json"
    assert bundle_check.main([str(export_zip), "--expect", str(expect), "--report", str(tmp_path / "r1.json")]) == 0
    saved = json.loads(expect.read_text(encoding="utf-8"))
    assert saved["rooms"]["room-1"]["name"] == "kitchen"
    assert saved["queries"]["the kitchen"] == "kitchen"

    # Someone renames a room in the expectation file: the run must notice.
    saved["rooms"]["room-1"]["name"] = "pantry"
    expect.write_text(json.dumps(saved), encoding="utf-8")
    code = bundle_check.main([str(export_zip), "--expect", str(expect), "--report", str(tmp_path / "r2.json")])
    assert code == 1
    checks = {c["check"]: c for c in _report(tmp_path / "r2.json")["checks"]}
    assert checks["expectations"]["status"] == "FAIL"
    assert "room-1: 'pantry' -> 'kitchen'" in checks["expectations"]["detail"]

    assert bundle_check.main([str(export_zip), "--expect", str(expect), "--update-expect",
                              "--report", str(tmp_path / "r3.json")]) == 0


def test_a_bundle_with_no_structure_fails_layout(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "storage"))
    base = tmp_path / "empty"
    (base / "rooms" / "room-1" / "rebuild").mkdir(parents=True)
    (base / "rooms" / "room-1" / "rebuild" / "manifest.json").write_text('{"frames": []}', encoding="utf-8")
    code = bundle_check.main([str(base), "--report", str(tmp_path / "r.json")])
    assert code == 1
    assert "no area has RoomPlan structure" in capsys.readouterr().out


def test_mismatched_poses_fail_the_frame_check(export_zip, tmp_path, monkeypatch):
    """Geometry and cameras in different spaces must be a FAIL, not a quiet
    empty selection."""
    import bundle_check as module

    from app.frame_select import ConventionError

    def broken(room, manifest, count):
        raise ConventionError("no object projects into any frame")

    monkeypatch.setattr("app.frame_select.select_frames", broken)
    code = module.main([str(export_zip), "--report", str(tmp_path / "r.json")])
    assert code == 1
    checks = {c["check"]: c for c in _report(tmp_path / "r.json")["checks"]}
    assert checks["frames"]["status"] == "FAIL"
    assert "not in the same space" in checks["frames"]["detail"]


def test_legacy_shape_validator_catches_a_missing_key():
    problems = bundle_check.validate_legacy_shape({"schemaVersion": "v1", "threadId": "t",
                                                   "message": {}, "state": {}, "suggestedReplies": [],
                                                   "model": "m", "provider": "p"})
    assert any("usedFallback" in p for p in problems)
    assert any("message.id" in p for p in problems)
