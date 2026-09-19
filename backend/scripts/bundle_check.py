#!/usr/bin/env python
"""Drop a scan export in, get a pass/fail out.

One command per bundle, no model call, no network, no money:

    python scripts/bundle_check.py <scan.zip | bundle_dir>
        [--home-id ID]              defaults to the scan id in meta.json
        [--expect FILE]             compare room names and query answers to a
                                    saved expectation file (see --update-expect)
        [--update-expect]           write/overwrite that file from this run
        [--frames N]                frames to select per room (default 4)
        [--report FILE]             JSON report (default: backend_storage/bundle_checks/<id>.json)
        [--live URL --ops-token T]  also PUT/GET the index against a deployed service

What it checks, in order:

  0  layout        the files ingest reads are where the app writes them
  1  resolution    every area gets a name, names are unique, frames are placed
  2  expectations  names, confidence and the standard queries match the file
  3  frames        PR #30 frame selection runs on every room that has objects
  4  measurements  paintable / floor / perimeter, cross-checked against the index
  5  models        the phone's textured bake per area: present, a valid USDZ,
                   and under the storage plan's size cap (over = WARN, the
                   lead package will say the model exists but was not stored)
  6  wire          ingest into a temp store, PUT/GET through the ops API, one
                   chat turn that names a room, and the response decodes on
                   the shipped iOS client
  7  live          optional: the same PUT/GET against a real deployment

Exit 0 when nothing FAILs. WARN never fails the run; it is printed for a
human. The report and expectation files land under backend_storage/, which
is gitignored, so a real home never gets committed (SOW section 12).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# The queries that matter most on a whole-home scan. What they resolve to is
# recorded in the expectation file, so a naming-rule change shows up as a diff.
STANDARD_QUERIES = (
    "master bathroom", "the kitchen", "living room", "my bedroom",
    "the garage", "where the washer is", "upstairs bathroom", "downstairs bathroom",
)

# The files the app writes per area (RoomPlanArchive.swift / RebuildArchive.swift).
AREA_STRUCTURE_FILES = ("room.json", "live.json")
AREA_FLOOR_FILE = "floor.json"
AREA_MANIFEST = Path("rebuild") / "manifest.json"
AREA_IMAGES = Path("rebuild") / "images"

FLOOR_AREA_TOLERANCE = 0.02       # index vs room_context, two independent paths
OPEN_VOLUME_WALL_HEIGHT_M = 3.2   # above this a room is probably open to a stairwell


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.rooms: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}

    def add(self, check: str, status: str, detail: str, **data: Any) -> None:
        self.checks.append({"check": check, "status": status, "detail": detail, **data})

    @property
    def failed(self) -> bool:
        return any(c["status"] == "FAIL" for c in self.checks)

    def to_json(self) -> dict[str, Any]:
        return {"meta": self.meta, "checks": self.checks, "rooms": self.rooms}


# --------------------------------------------------------------------------
# 0. unpack + layout
# --------------------------------------------------------------------------
def unpack(source: Path, workdir: Path) -> Path:
    """A zip or a directory -> the directory that holds ``rooms/``.

    Exports arrive zipped from a phone, sometimes with the ``Scans/<id>/``
    parents included, so the root is searched for rather than assumed.
    """
    if source.is_dir():
        root = source
    else:
        target = workdir / "unpacked"
        with zipfile.ZipFile(source) as zf:
            zf.extractall(target)
        root = target
    if (root / "rooms").is_dir():
        return root
    for candidate in sorted(root.rglob("rooms")):
        if candidate.is_dir() and any(p.name.startswith("room-") for p in candidate.iterdir()):
            return candidate.parent
    return root


def check_layout(root: Path, report: Report) -> list[Path]:
    rooms_dir = root / "rooms"
    if not rooms_dir.is_dir():
        report.add("layout", "FAIL", f"no rooms/ directory under {root}")
        return []
    area_dirs = sorted(
        (p for p in rooms_dir.glob("room-*") if p.is_dir()),
        key=lambda p: int(p.name.split("-")[-1]),
    )
    if not area_dirs:
        report.add("layout", "FAIL", "rooms/ has no room-N directories")
        return []

    problems: list[str] = []
    if not (root / "meta.json").exists():
        problems.append("meta.json missing (home id will fall back to the folder name)")
    missing_structure = [d.name for d in area_dirs if not any((d / f).exists() for f in AREA_STRUCTURE_FILES)]
    missing_floor = [d.name for d in area_dirs if not (d / AREA_FLOOR_FILE).exists()]
    missing_manifest = [d.name for d in area_dirs if not (d / AREA_MANIFEST).exists()]
    missing_images = 0
    frame_total = 0
    for d in area_dirs:
        manifest = _load_json(d / AREA_MANIFEST) or {}
        for frame in manifest.get("frames") or []:
            frame_total += 1
            fid = frame.get("id")
            if fid and not (d / AREA_IMAGES / f"{fid}.jpg").exists():
                missing_images += 1
    if missing_structure:
        problems.append(f"no room.json/live.json in {missing_structure} (these areas stay unnamed)")
    if missing_floor:
        problems.append(f"no floor.json in {missing_floor} (storey defaults to 0)")
    if missing_manifest:
        problems.append(f"no rebuild/manifest.json in {missing_manifest} (no frames for those areas)")
    if missing_images:
        problems.append(f"{missing_images} of {frame_total} manifest frames have no image file "
                        "(frame selection still runs; the appearance pass cannot)")

    status = "WARN" if problems else "PASS"
    if len(missing_structure) == len(area_dirs):
        status = "FAIL"
        problems.insert(0, "no area has RoomPlan structure at all")
    report.add("layout", status, "; ".join(problems) or f"{len(area_dirs)} areas, {frame_total} frames",
               areas=len(area_dirs), frames=frame_total)
    report.meta["frameTotal"] = frame_total
    return area_dirs


# --------------------------------------------------------------------------
# 1. resolution
# --------------------------------------------------------------------------
def check_resolution(root: Path, report: Report):
    from app.home_index import load_bundle

    started = time.perf_counter()
    index = load_bundle(root)
    elapsed = time.perf_counter() - started
    report.meta["bundleId"] = index.bundle_id
    report.meta["resolutionSeconds"] = round(elapsed, 3)

    problems: list[str] = []
    fails: list[str] = []
    names = [r.display_name for r in index.rooms]
    if not index.rooms:
        fails.append("no rooms resolved")
    if any(not n for n in names):
        fails.append("a room has no display name")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        fails.append(f"duplicate room names {duplicates}")
    zero_area = [r.key for r in index.rooms if r.polygon and r.area_sqft <= 0]
    if zero_area:
        fails.append(f"zero floor area with a polygon present in {zero_area}")
    placed = sum(len(r.frame_ids) for r in index.rooms)
    if report.meta.get("frameTotal") and placed != report.meta["frameTotal"]:
        fails.append(f"{placed} frames placed of {report.meta['frameTotal']} in manifests")
    confident = sum(1 for r in index.rooms if r.confident)
    if index.rooms and confident < math.ceil(len(index.rooms) / 2):
        problems.append(f"only {confident} of {len(index.rooms)} rooms named confidently")
    unnamed = [r.display_name for r in index.rooms if r.role == "unknown"]
    if unnamed:
        problems.append(f"needs the homeowner: {unnamed}")

    for r in sorted(index.rooms, key=lambda x: (x.storey, -x.area_sqft)):
        report.rooms.append({
            "key": r.key, "name": r.display_name, "confident": r.confident, "role": r.role,
            "storey": r.storey, "areaSqFt": round(r.area_sqft, 1), "frames": len(r.frame_ids),
            "basis": r.name_basis,
        })
    status = "FAIL" if fails else ("WARN" if problems else "PASS")
    report.add("resolution", status, "; ".join(fails + problems) or
               f"{len(index.rooms)} rooms, {confident} confident, {placed} frames placed, {elapsed:.2f}s",
               rooms=len(index.rooms), confident=confident, framesPlaced=placed)
    return index


# --------------------------------------------------------------------------
# 2. expectations
# --------------------------------------------------------------------------
def capture_expectations(index) -> dict[str, Any]:
    return {
        "roomCount": len(index.rooms),
        "storeys": index.storey_count,
        "rooms": {
            r.key: {"name": r.display_name, "confident": r.confident, "role": r.role,
                    "areaSqFt": round(r.area_sqft)}
            for r in sorted(index.rooms, key=lambda x: x.key)
        },
        "queries": {q: (index.resolve(q).display_name if index.resolve(q) else None)
                    for q in STANDARD_QUERIES},
    }


def diff_expectations(before: dict, after: dict) -> list[str]:
    changes: list[str] = []
    for key in sorted(set(before.get("rooms", {})) | set(after.get("rooms", {}))):
        b = before.get("rooms", {}).get(key, {})
        a = after.get("rooms", {}).get(key, {})
        if b.get("name") != a.get("name"):
            changes.append(f"{key}: '{b.get('name')}' -> '{a.get('name')}'")
        elif b.get("confident") != a.get("confident"):
            changes.append(f"{key} ({a.get('name')}): confident {b.get('confident')} -> {a.get('confident')}")
    for q in sorted(set(before.get("queries", {})) | set(after.get("queries", {}))):
        if before.get("queries", {}).get(q) != after.get("queries", {}).get(q):
            changes.append(f"resolve({q!r}): {before.get('queries', {}).get(q)!r} "
                           f"-> {after.get('queries', {}).get(q)!r}")
    return changes


def check_expectations(index, expect: Path | None, update: bool, report: Report) -> None:
    current = capture_expectations(index)
    report.meta["queries"] = current["queries"]
    if expect is None:
        report.add("expectations", "PASS", "no expectation file given (pass --expect to pin names)")
        return
    if update or not expect.exists():
        expect.parent.mkdir(parents=True, exist_ok=True)
        expect.write_text(json.dumps(current, indent=2), encoding="utf-8")
        report.add("expectations", "PASS", f"{'updated' if update else 'wrote'} baseline {expect}")
        return
    previous = json.loads(expect.read_text(encoding="utf-8"))
    changes = diff_expectations(previous, current)
    if changes:
        report.add("expectations", "FAIL",
                   f"{len(changes)} change(s) against {expect.name}: " + "; ".join(changes) +
                   " (intended? re-run with --update-expect)", changes=changes)
    else:
        report.add("expectations", "PASS", f"matches {expect.name}")


# --------------------------------------------------------------------------
# 3. frames
# --------------------------------------------------------------------------
def check_frames(root: Path, index, report: Report, count: int) -> None:
    from app.frame_select import ConventionError, frame_records, object_boxes, select_frames

    started = time.perf_counter()
    ran = 0
    skipped: list[str] = []
    fails: list[str] = []
    warns: list[str] = []
    per_room: dict[str, Any] = {}
    for room in index.rooms:
        room_dir = root / "rooms" / room.key
        structure = _load_json(room_dir / "room.json") or _load_json(room_dir / "live.json") or {}
        manifest = _load_json(room_dir / AREA_MANIFEST) or {}
        boxes = object_boxes(structure)
        frames = frame_records(manifest)
        if not boxes or not frames:
            skipped.append(room.key)
            continue
        try:
            selected = select_frames(structure, manifest, count)
        except ConventionError as exc:
            fails.append(f"{room.key} ({room.display_name}): {exc} "
                         "-- geometry and camera poses are not in the same space")
            continue
        ran += 1
        if not selected:
            warns.append(f"{room.key} ({room.display_name}): objects and frames present, nothing selected")
        turns = {f["turns"] for f in selected}
        if len(turns) > 1:
            warns.append(f"{room.key} ({room.display_name}): selected frames disagree on upright "
                         f"rotation {sorted(turns)} -- a pose is probably wrong")
        missing = [f["id"] for f in selected if not (room_dir / AREA_IMAGES / f"{f['id']}.jpg").exists()]
        if missing:
            warns.append(f"{room.key}: {len(missing)} selected frame(s) have no image file")
        per_room[room.key] = [f["id"] for f in selected]
    elapsed = time.perf_counter() - started
    for row in report.rooms:
        row["selectedFrames"] = per_room.get(row["key"], [])
    if fails:
        status = "FAIL"
    elif warns:
        status = "WARN"
    else:
        status = "PASS"
    detail = "; ".join(fails + warns) or f"selected frames for {ran} room(s) in {elapsed:.2f}s"
    if skipped:
        detail += f"; skipped {len(skipped)} with no objects or no posed frames"
    report.add("frames", status, detail, ran=ran, skipped=skipped, seconds=round(elapsed, 3))


# --------------------------------------------------------------------------
# 4. measurements
# --------------------------------------------------------------------------
def check_measurements(root: Path, index, report: Report) -> None:
    from app.room_context import measurements_from_geometry

    fails: list[str] = []
    warns: list[str] = []
    measured = 0
    for row in report.rooms:
        room = index.by_key(row["key"])
        room_dir = root / "rooms" / room.key
        structure = _load_json(room_dir / "room.json") or _load_json(room_dir / "live.json") or {}
        m = measurements_from_geometry(structure)
        row["measurements"] = m
        if not m:
            continue
        measured += 1
        floor = m.get("floor_sqft")
        if floor and room.area_sqft:
            drift = abs(floor - room.area_sqft) / room.area_sqft
            if drift > FLOOR_AREA_TOLERANCE:
                fails.append(f"{room.key}: floor {floor} sq ft (room_context) vs {room.area_sqft:.1f} "
                             f"(home_index), {drift:.1%} apart")
        height = m.get("mean_wall_height_m")
        if height and height > OPEN_VOLUME_WALL_HEIGHT_M:
            warns.append(f"{room.key} ({room.display_name}): walls average {height} m, "
                         "open volume suspected, paintable area is an upper bound")
    status = "FAIL" if fails else ("WARN" if warns else "PASS")
    report.add("measurements", status, "; ".join(fails + warns) or f"{measured} room(s) measured",
               measured=measured)


# --------------------------------------------------------------------------
# 5. models -- the on-device textured bakes
# --------------------------------------------------------------------------
def _usdz_ok(path: Path) -> tuple[bool, str]:
    """A USDZ is a zip whose first member is the USD file. Anything else --
    a truncated upload, an HTML error page saved as .usdz -- would render
    as nothing in the provider's viewer, so it is worth ten milliseconds."""
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile:
        return False, "not a zip"
    if not names:
        return False, "empty archive"
    if not any(n.lower().endswith((".usdc", ".usda", ".usd")) for n in names):
        return False, "no USD file inside"
    return True, ""


def check_models(root: Path, index, report: Report) -> None:
    """Every area's ``model.usdz`` plus the whole-home one. Missing is a WARN
    (texturing had not finished when the homeowner exported -- the first
    whole-home export had 1 of 19), invalid is a FAIL, over the storage cap
    is a WARN with the exact size so the plan can be raised deliberately."""
    from app.config import settings

    cap_mb = int(settings.model_upload_max_mb)
    fails: list[str] = []
    warns: list[str] = []
    present = 0
    over = 0
    for row in report.rooms:
        room = index.by_key(row["key"])
        path = root / "rooms" / room.key / "model.usdz"
        if not path.is_file():
            row["model"] = {"present": False}
            warns.append(f"{room.key} ({room.display_name}): no model.usdz (not textured before export)")
            continue
        size = path.stat().st_size
        ok, why = _usdz_ok(path)
        row["model"] = {"present": True, "bytes": size, "valid": ok,
                        "overCap": size > cap_mb * 1048576}
        present += 1
        if not ok:
            fails.append(f"{room.key}: model.usdz unreadable ({why})")
        if size > cap_mb * 1048576:
            over += 1
            warns.append(f"{room.key} ({room.display_name}): model is {size / 1048576:.0f} MB, "
                         f"over LIDARAI_MODEL_UPLOAD_MAX_MB={cap_mb}; it will not be stored "
                         "and the lead package will say so")
    home = root / "model.usdz"
    if home.is_file():
        size = home.stat().st_size
        ok, why = _usdz_ok(home)
        report.meta["homeModel"] = {"bytes": size, "valid": ok, "overCap": size > cap_mb * 1048576}
        if not ok:
            fails.append(f"whole-home model.usdz unreadable ({why})")
        elif size > cap_mb * 1048576:
            warns.append(f"whole-home model is {size / 1048576:.0f} MB, over the {cap_mb} MB cap")
    else:
        report.meta["homeModel"] = None
    status = "FAIL" if fails else ("WARN" if warns else "PASS")
    detail = "; ".join(fails + warns) or f"{present} room model(s), all valid and under {cap_mb} MB"
    report.add("models", status, detail, present=present, overCap=over,
               rooms=len(report.rooms))


# --------------------------------------------------------------------------
# 6. wire (offline, in-process)
# --------------------------------------------------------------------------
def _load_fixture() -> dict[str, Any]:
    return json.loads((BACKEND / "tests" / "fixtures" / "legacy_ios_decoder.json").read_text(encoding="utf-8"))


def validate_legacy_shape(body: dict[str, Any]) -> list[str]:
    """The shipped iOS decoder's required keys, types and enums. Returns the
    problems; an empty list means the app would decode this response."""
    fixture = _load_fixture()
    problems: list[str] = []

    def check_type(value, spec: str, path: str) -> None:
        if spec.startswith("enum:"):
            allowed = fixture["enums"][spec.split(":", 1)[1]]
            if value not in allowed:
                problems.append(f"{path}: {value!r} not in {allowed}")
            return
        base = spec.split("<", 1)[0]
        expected = {"string": str, "boolean": bool, "object": dict, "array": list,
                    "number": (int, float)}[base]
        if not isinstance(value, expected):
            problems.append(f"{path}: expected {spec}, got {type(value).__name__}")
        elif spec == "array<string>" and not all(isinstance(i, str) for i in value):
            problems.append(f"{path}: array has non-string items")

    def section(payload: dict, name: str, path: str) -> None:
        spec = fixture[name]
        for key, type_spec in spec.get("required", {}).items():
            if key not in payload or payload[key] is None:
                problems.append(f"{path}.{key}: missing or null (the iOS decoder hard-fails)")
                continue
            check_type(payload[key], type_spec, f"{path}.{key}")
        for key, type_spec in spec.get("optional", {}).items():
            if payload.get(key) is not None:
                check_type(payload[key], type_spec, f"{path}.{key}")

    section(body, "response", "response")
    if isinstance(body.get("message"), dict):
        section(body["message"], "message", "message")
    if isinstance(body.get("state"), dict):
        section(body["state"], "state", "state")
    for optional in ("quoteDraft", "visualFocus"):
        if isinstance(body.get(optional), dict):
            section(body[optional], optional, optional)
    return problems


async def check_wire(root: Path, home_id: str, workdir: Path, report: Report) -> None:
    from app.config import settings

    # Hermetic: no provider, no Supabase, no email. The turn runs on the
    # deterministic local fallback, which is the shape every production turn
    # degrades to, so it is the right one to check the decoder against.
    # Settings are restored afterwards so the check leaves no trace in the
    # process that ran it (pytest runs it in-process).
    overrides = {name: "" for name in (
        "supabase_url", "supabase_service_role_key", "supabase_jwt_secret",
        "anthropic_api_key", "openai_api_key", "ops_email", "ops_webhook_url",
        "resend_api_key", "smtp_host")}
    overrides["storage_dir"] = str(workdir / "storage")
    overrides["ops_token"] = settings.ops_token or "bundle-check"
    saved = {name: getattr(settings, name) for name in overrides}
    for name, value in overrides.items():
        setattr(settings, name, value)
    try:
        await _wire_checks(root, home_id, report)
    finally:
        for name, value in saved.items():
            setattr(settings, name, value)


async def _wire_checks(root: Path, home_id: str, report: Report) -> None:
    from httpx import ASGITransport, AsyncClient

    from app.config import settings
    from app.flow import home_registry
    from app.main import app

    home_registry._cache.clear()
    index = home_registry.ingest_bundle(root, home_id)
    target = next((r for r in index.rooms if r.confident), index.rooms[0] if index.rooms else None)
    if target is None:
        report.add("wire", "FAIL", "no rooms to talk about")
        return

    auth = {"Authorization": f"Bearer {settings.auth_token}"} if settings.auth_token else {}
    ops = {"Authorization": f"Bearer {settings.ops_token}"}
    fails: list[str] = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://bundle-check") as http:
        put = await http.put(f"/api/v1/ops/homes/{home_id}", json=index.to_json(), headers=ops)
        if put.status_code != 200:
            fails.append(f"PUT /ops/homes -> {put.status_code} {put.text[:200]}")
        elif put.json().get("roomCount") != len(index.rooms):
            fails.append(f"PUT /ops/homes stored {put.json().get('roomCount')} rooms, expected {len(index.rooms)}")
        got = await http.get(f"/api/v1/ops/homes/{home_id}", headers=ops)
        if got.status_code != 200 or got.json().get("roomCount") != len(index.rooms):
            fails.append(f"GET /ops/homes -> {got.status_code}")

        turn = await http.post(
            "/api/v1/ai/home-chat",
            json={"threadId": f"bundle-check-{home_id}", "message": f"let's do the {target.display_name}",
                  "homeId": home_id},
            headers=auth,
        )
        if turn.status_code != 200:
            fails.append(f"POST /ai/home-chat -> {turn.status_code} {turn.text[:200]}")
        else:
            body = turn.json()
            fails.extend(validate_legacy_shape(body))
            home = (body.get("flow") or {}).get("home") or {}
            active = (home.get("activeRoom") or {}).get("key")
            if not home:
                fails.append("response has no flow.home: the index was not attached to the turn")
            elif active != target.key:
                fails.append(f"'let's do the {target.display_name}' resolved to {active!r}, expected {target.key!r}"
                             + (f" (unresolvedRoom={home.get('unresolvedRoom')!r})" if home.get("unresolvedRoom") else ""))

        gone = await http.delete(f"/api/v1/ops/homes/{home_id}", headers=ops)
        if gone.status_code != 200:
            fails.append(f"DELETE /ops/homes -> {gone.status_code}")
        after = await http.get(f"/api/v1/ops/homes/{home_id}", headers=ops)
        if after.status_code != 404:
            fails.append(f"GET after DELETE -> {after.status_code}, expected 404")
    if home_registry.room_context_for(home_id, target.key) is not None:
        fails.append("room context survived DELETE (partial deletion, SOW section 12)")
    home_registry._cache.clear()

    report.add("wire", "FAIL" if fails else "PASS", "; ".join(fails) or
               f"PUT/GET/turn/DELETE ok; '{target.display_name}' resolved to {target.key}; "
               "response decodes on the shipped iOS client",
               targetRoom=target.key)


# --------------------------------------------------------------------------
# 6. live (optional)
# --------------------------------------------------------------------------
def check_live(index, home_id: str, url: str, ops_token: str, report: Report) -> None:
    import httpx

    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {ops_token}"}
    fails: list[str] = []
    try:
        with httpx.Client(timeout=60.0) as http:
            put = http.put(f"{base}/api/v1/ops/homes/{home_id}", json=index.to_json(), headers=headers)
            if put.status_code != 200:
                fails.append(f"PUT -> {put.status_code} {put.text[:200]}")
            got = http.get(f"{base}/api/v1/ops/homes/{home_id}", headers=headers)
            if got.status_code != 200 or got.json().get("roomCount") != len(index.rooms):
                fails.append(f"GET -> {got.status_code}")
    except Exception as exc:  # noqa: BLE001
        fails.append(f"{type(exc).__name__}: {exc}")
    report.add("live", "FAIL" if fails else "PASS", "; ".join(fails) or
               f"index stored and read back at {base} (left in place for the conversation)")


# --------------------------------------------------------------------------
def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _print(report: Report) -> None:
    width = max(len(c["check"]) for c in report.checks) if report.checks else 12
    print(f"bundle {report.meta.get('bundleId', '?')}  home {report.meta.get('homeId', '?')}")
    for c in report.checks:
        print(f"  [{c['status']:<4}] {c['check']:<{width}}  {c['detail']}")
    if report.rooms:
        print()
        print(f"  {'room':<26} {'key':<9} {'sqft':>6} {'paint':>7} {'frames':>6} {'model':>6}  basis")
        for r in report.rooms:
            m = r.get("measurements") or {}
            paint = m.get("paintable_sqft")
            mark = " " if r["confident"] else "?"
            model = r.get("model") or {}
            if not model.get("present"):
                model_col = "    --"
            else:
                model_col = f"{model['bytes'] / 1048576:>4.0f}MB" + ("!" if model.get("overCap") else "")
            print(f"{mark} {r['name']:<26} {r['key']:<9} {r['areaSqFt']:>6.0f} "
                  f"{(f'{paint:.0f}' if paint is not None else '--'):>7} {r['frames']:>6} {model_col:>6}  {r['basis']}")
    print()
    print("RESULT: " + ("FAIL" if report.failed else "PASS"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bundle_check", description=__doc__.split("\n\n")[0])
    parser.add_argument("source", help="scan export zip, or the unzipped directory")
    parser.add_argument("--home-id", default=None)
    parser.add_argument("--expect", default=None, help="expectation file to compare against")
    parser.add_argument("--update-expect", action="store_true")
    parser.add_argument("--frames", type=int, default=4)
    parser.add_argument("--report", default=None)
    parser.add_argument("--live", default=None, help="deployed base URL for the optional live check")
    parser.add_argument("--ops-token", default=None)
    parser.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = parser.parse_args(argv)

    source = Path(args.source)
    if not source.exists():
        print(f"no such file or directory: {source}", file=sys.stderr)
        return 2
    workdir = Path(tempfile.mkdtemp(prefix="bundle-check-"))
    report = Report()
    try:
        root = unpack(source, workdir)
        report.meta["source"] = str(source)
        check_layout(root, report)
        if report.failed:
            _print(report)
            return 1
        index = check_resolution(root, report)
        home_id = (args.home_id or index.bundle_id or root.name).strip()
        report.meta["homeId"] = home_id
        check_expectations(index, Path(args.expect) if args.expect else None, args.update_expect, report)
        check_frames(root, index, report, max(1, args.frames))
        check_measurements(root, index, report)
        check_models(root, index, report)
        asyncio.run(check_wire(root, home_id, workdir, report))
        if args.live:
            if not args.ops_token:
                report.add("live", "FAIL", "--live needs --ops-token")
            else:
                check_live(index, home_id, args.live, args.ops_token, report)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    out = Path(args.report) if args.report else BACKEND / "backend_storage" / "bundle_checks" / f"{report.meta.get('homeId', 'bundle')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
    _print(report)
    print(f"report: {out}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
