"""Where a resolved home index lives between the scan and the conversation.

Ingestion is a one-off: a walked-home export is hundreds of megabytes, and
none of it belongs on the chat server (SOW section 12). What the agent
actually needs is the resolved index -- named rooms, footprints, fixtures,
frame ids -- which is a few tens of kilobytes and carries no photos, no
depth, and no mesh.

So the bundle is ingested once, the index is stored under a ``home_id``,
and every turn reads it from there. Local file first (fast, and the only
copy when Supabase is unconfigured); Supabase when configured, so the
index survives a redeploy the same way flow state does.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import re
from pathlib import Path

from ..config import settings
from ..home_index import HomeIndex, load_bundle

logger = logging.getLogger("lidarai.flow.home_registry")

# Small and read every turn: keep the parsed index in memory.
_cache: dict[str, HomeIndex] = {}
# Strong refs for in-flight durable writes.
_pending: set = set()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(home_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", home_id)[:120]


def _path(home_id: str) -> Path:
    base = Path(settings.storage_dir) / "homes"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{_safe(home_id)}.json"


def save_index(home_id: str, index: HomeIndex, *, durable: bool = True) -> Path:
    """Local file first (fast, and the only copy when Supabase is off), then
    the durable copy. The host's disk is ephemeral: without the durable
    write, every redeploy wiped the home and the demo answered "that home
    has not been ingested" until it was uploaded again."""
    payload = index.to_json()
    path = _path(home_id)
    path.write_text(json.dumps(payload), encoding="utf-8")
    _cache[home_id] = index
    if durable:
        _durable_write(home_id, payload)
    logger.info("Stored home index %s (%d rooms)", home_id, len(index.rooms))
    return path


def _durable_write(home_id: str, payload: dict) -> None:
    """Fire-and-forget so ingestion never blocks on Supabase; falls back to
    a synchronous write when there is no running loop (CLI ingest)."""
    from . import supabase_store

    if not supabase_store.enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(supabase_store.put_home_index(home_id, payload))
        return
    task = loop.create_task(supabase_store.put_home_index(home_id, payload))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def load_index(home_id: str | None) -> HomeIndex | None:
    """Memory, then local disk. A cold instance whose disk was wiped
    rehydrates from the durable copy via `load_index_async`."""
    if not home_id:
        return None
    cached = _cache.get(home_id)
    if cached is not None:
        return cached
    path = _path(home_id)
    if not path.exists():
        return None
    try:
        index = HomeIndex.from_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read home index %s: %s", home_id, exc)
        return None
    _cache[home_id] = index
    return index


async def load_index_async(home_id: str | None, *, refresh: bool = False) -> HomeIndex | None:
    """Same, but falls back to the durable copy — this is what makes a home
    survive a redeploy."""
    if not home_id:
        return None
    local = load_index(home_id)
    if local is not None and not refresh:
        return local
    from . import supabase_store

    payload = await supabase_store.get_home_index(home_id)
    # An ingestion or model registration may have published a newer index
    # while this read was in flight. Never overwrite it with that older read.
    current = load_index(home_id)
    if current is not None and current is not local:
        return current
    if not payload:
        return local
    try:
        index = HomeIndex.from_json(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Bad durable home index %s: %s", home_id, exc)
        return None
    # Re-warm the local copy so later turns are a memory hit.
    try:
        _path(home_id).write_text(json.dumps(payload), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    _cache[home_id] = index
    logger.info("Rehydrated home index %s from durable storage", home_id)
    return index


def unpack_export(source: Path, workdir: Path) -> Path:
    """A zip or a directory -> the directory that holds ``rooms/``.

    Exports arrive zipped from a phone, sometimes with the ``Scans/<id>/``
    parents included, so the root is searched for rather than assumed
    (same rule as scripts/bundle_check.py).
    """
    import zipfile

    if source.is_dir():
        root = source
    else:
        root = workdir / "unpacked"
        with zipfile.ZipFile(source) as zf:
            zf.extractall(root)
    if (root / "rooms").is_dir():
        return root
    for candidate in sorted(root.rglob("rooms")):
        if candidate.is_dir() and any(p.name.startswith("room-") for p in candidate.iterdir()):
            return candidate.parent
    return root


# Ingest runs in the background; this is what a status call can see. Per
# process, deliberately: the durable answer is whether the index exists.
_ingest_status: dict[str, dict] = {}


def ingest_status(home_id: str) -> dict | None:
    return _ingest_status.get(home_id)


async def ingest_from_storage(
    home_id: str, bucket: str, object_path: str, *, enrich: bool = True,
    upload_revision: str | None = None, owner_id: str | None = None
) -> dict:
    """Pull an export the app uploaded to Supabase Storage and ingest it here.

    Until now a whole-home scan reached the agent only when a person with
    the bundle ran the CLI on a laptop (Sep 15 audit: "nobody owns bundle
    handling"). The app already uploads its export zip to the
    ``metashape-exports`` bucket; this closes the loop server-side: download,
    unpack, resolve the index, copy the models to durable storage, and --
    when a model key is configured -- run the appearance pass so the agent
    can name floors and walls. The zip is deleted afterwards; the index and
    the room documents are what survive.
    """
    import shutil
    import tempfile

    from . import supabase_store

    status = {"status": "running", "stage": "reading_scan", "bucket": bucket,
              "objectPath": object_path, "revision": upload_revision}
    _ingest_status[home_id] = status
    workdir = Path(tempfile.mkdtemp(prefix=f"ingest-{home_id}-", dir=_ingest_workroot()))
    try:
        archive = workdir / "export.zip"
        if not await supabase_store.download_object(bucket, object_path, archive):
            raise RuntimeError(f"could not download {bucket}/{object_path}")
        bundle_dir = await asyncio.to_thread(unpack_export, archive, workdir)
        previous = await load_index_async(home_id)
        index = await asyncio.to_thread(ingest_bundle, bundle_dir, home_id, require_rooms=True)
        # Metadata refreshes must keep homeowner names and finished models.
        # A new revision still waits for a new final-model registration.
        if previous is not None:
            for room in index.rooms:
                old = previous.by_key(room.key)
                if old is not None:
                    if not room.model:
                        room.model = dict(old.model)
                    if old.named_by_homeowner:
                        index.rename_room(room.key, old.display_name)
            if not index.home_model:
                index.home_model = dict(previous.home_model)
        if upload_revision:
            index.upload = {"revision": upload_revision, "ownerId": owner_id,
                            "contextObject": object_path, "modelsReady": False}
        elif previous is not None:
            index.upload = dict(previous.upload)
        save_index(home_id, index)

        status["roomCount"] = len(index.rooms)
        if enrich and settings.anthropic_api_key:
            status.update(stage="analyzing_photos", completedRooms=0, totalRooms=len(index.rooms))
            try:
                enriched = await enrich_rooms(bundle_dir, home_id, refresh=bool(upload_revision), progress=status)
                status["enrichedRooms"] = len(enriched)
            except Exception as exc:  # noqa: BLE001 -- the index is worth keeping without it
                logger.warning("Appearance pass failed for %s: %s", home_id, exc)
                status["enrichError"] = str(exc)[:200]
        if upload_revision:
            status["stage"] = "saving_context"
            ready = HomeIndex.from_json(index.to_json())
            ready.upload["contextReady"] = True
            await save_index_confirmed(home_id, ready)
        status["status"] = "done"
        status["stage"] = "ready"
        logger.info("Ingested %s from %s/%s: %d room(s)", home_id, bucket, object_path, len(index.rooms))
    except Exception as exc:  # noqa: BLE001
        status["status"] = "failed"
        status["error"] = str(exc)[:300]
        logger.warning("Ingest of %s from %s/%s failed: %s", home_id, bucket, object_path, exc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return status


def _ingest_workroot() -> str:
    root = Path(settings.storage_dir) / "ingest"
    root.mkdir(parents=True, exist_ok=True)
    return str(root)


def ingest_bundle(bundle_dir: str | Path, home_id: str | None = None, *, require_rooms: bool = False) -> HomeIndex:
    """Resolve an export into a named index and store it under ``home_id``.

    ``home_id`` defaults to the scan's own id (``meta.json`` ``id``, which is
    also the ``Scans/<id>/`` folder name on the device and the ``scanId`` the
    app sends in ``scanContext``), so the app and the server agree on the
    name of a home without anyone typing it.

    Measurements for every room -- paintable area, floor area, perimeter --
    are computed here and attached to the index BEFORE it is saved, because
    the bundle is on disk exactly once and the index is the durable copy
    (Supabase Storage). The local per-room context documents are a cache of
    the same numbers plus, optionally, the appearance pass; they can be lost
    to a redeploy without losing anything that cannot be rebuilt.

    Pure arithmetic: no model call, no network, no image read, so the bundle
    check can run it with no keys. The appearance pass (a model call per room)
    is ``enrich_rooms``, which the CLI runs by default.
    """
    index = load_bundle(bundle_dir)
    home_id = (home_id or index.bundle_id or "").strip()
    if not home_id:
        raise ValueError("home_id is required when the bundle has no meta.json id")
    if require_rooms and not index.rooms:
        raise ValueError("The scan export has no saved rooms")
    store_room_geometry(bundle_dir, home_id, index)
    store_home_models(bundle_dir, home_id, index)
    save_index(home_id, index)
    return index


def store_home_models(bundle_dir: str | Path, home_id: str, index: HomeIndex) -> dict[str, str]:
    """Copy each baked model the export carried into Supabase Storage and
    record the outcome on the index, BEFORE the index is saved.

    The 3D model link in a lead package (SOW section 2 step 9) has to
    outlive the export, which is deleted after ingest, and the host's disk,
    which is wiped on deploy. Storage is the only copy that does.

    Returns ``{key: outcome}`` with outcome ``stored`` / ``skipped: ...`` /
    ``failed`` / ``no storage``. A model over ``LIDARAI_MODEL_UPLOAD_MAX_MB``
    is not attempted -- the bucket would refuse it -- and the reason is kept
    on the index so the lead package can say so instead of pretending there
    is no model.
    """
    from . import supabase_store

    base = Path(bundle_dir)
    targets: list[tuple[str, dict]] = [(r.key, r.model) for r in index.rooms if r.model]
    if index.home_model:
        targets.append(("home", index.home_model))
    outcomes: dict[str, str] = {}
    if not targets:
        return outcomes
    cap_bytes = max(1, int(settings.model_upload_max_mb)) * 1024 * 1024

    async def _upload_all() -> None:
        for key, record in targets:
            record.pop("object", None)
            record.pop("skipped", None)
            size = int(record.get("bytes") or 0)
            if size > cap_bytes:
                record["skipped"] = (
                    f"{size / 1048576:.0f} MB exceeds LIDARAI_MODEL_UPLOAD_MAX_MB="
                    f"{settings.model_upload_max_mb}"
                )
                outcomes[key] = "skipped: " + record["skipped"]
                continue
            if not supabase_store.enabled():
                record["skipped"] = "no durable storage configured on this host"
                outcomes[key] = "no storage"
                continue
            ok = await supabase_store.put_home_model(home_id, key, base / record["file"])
            if ok:
                record["object"] = supabase_store.home_model_object(home_id, key)
                record["uploadedAt"] = now_iso()
                outcomes[key] = "stored"
            else:
                record["skipped"] = "upload failed (see server log)"
                outcomes[key] = "failed"

    _run_blocking(_upload_all())
    stored = sum(1 for v in outcomes.values() if v == "stored")
    logger.info("Home models for %s: %d stored, %d not (%s)", home_id, stored,
                len(outcomes) - stored,
                ", ".join(f"{k}={v}" for k, v in outcomes.items() if v != "stored") or "-")
    return outcomes


def _run_blocking(coro) -> None:
    """Run a coroutine to completion from sync code. Ingest is an operator
    command, never a request handler, so blocking is the honest choice: the
    index must carry the upload outcome before it is saved. Inside a running
    loop (the bundle check, tests) the work moves to a helper thread with its
    own loop rather than being scheduled after the bundle is gone."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    import threading

    error: list[BaseException] = []

    def _target() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=_target, name="home-model-upload", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]


def store_room_geometry(
    bundle_dir: str | Path, home_id: str, index: HomeIndex
) -> int:
    """Measurements for every room in the index. Returns how many were stored.

    Attaches each room's measurements to the index object (so a following
    ``save_index`` carries them to durable storage) and writes the local
    context document. A room that fails to measure must not take the ingest
    down with it -- the index is the thing the conversation cannot run
    without.
    """
    from .. import room_context

    stored = 0
    for room in index.rooms:
        try:
            document = room_context.geometry_context(bundle_dir, room.key)
            room.measurements = dict(document.get("measurements") or {})
            room_context.save(home_id, document)
            stored += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("No geometry context for %s/%s: %s", home_id, room.key, exc)
    logger.info("Stored %d room contexts for home %s", stored, home_id)
    return stored


async def enrich_room(
    bundle_dir: str | Path, home_id: str, room_key: str, *, refresh: bool = False
) -> dict:
    """Run the appearance pass for ONE room and cache it.

    Kept out of ``ingest_bundle`` so ingest stays free of keys and network,
    but the CLI runs it for every room by default. It used to be opt-in to
    save model calls, and Quintin (Sep 14) asked about his walls and floors on
    a home that had skipped it: the agent could only say it could not see them.
    """
    from .. import room_context

    document = await room_context.get(
        bundle_dir, home_id, room_key, refresh=refresh
    )
    return document


async def enrich_rooms(
    bundle_dir: str | Path,
    home_id: str,
    room_keys: list[str] | None = None,
    *,
    refresh: bool = False,
    progress: dict | None = None,
) -> dict[str, dict]:
    """The appearance pass for several rooms (all of them when ``room_keys``
    is None), one model call each, sequentially. Returns ``{room_key:
    document}``. This is the operator entry point: the bundle only exists on
    the machine that ran ingest, so the pass has to run there too."""
    index = load_index(home_id) or load_bundle(bundle_dir)
    keys = room_keys or [room.key for room in index.rooms]
    out: dict[str, dict] = {}
    for key in keys:
        out[key] = await enrich_room(bundle_dir, home_id, key, refresh=refresh)
        if progress is not None:
            progress["completedRooms"] = len(out)
    # The documents are cached on this machine's disk, which the export is on
    # and the server is not. Copying them onto the index is what actually
    # delivers them: the index is the object that gets uploaded, and the one
    # that survives a redeploy. Without this the agent is back to geometry
    # with no surfaces on it, which is the gap Quintin hit on Sep 11.
    stored = 0
    for key, document in out.items():
        room = index.by_key(key)
        if room is not None and document:
            room.appearance = dict(document)
            stored += 1
    if stored:
        save_index(home_id, index)
        logger.info("Appearance rides on the index for %s: %d rooms", home_id, stored)
    return out


def room_context_for(home_id: str | None, room_key: str | None) -> dict | None:
    """The stored context for a room, or None. Cheap enough to call per turn.

    Local document first. When the disk has been wiped (a redeploy) the
    measurements are rebuilt from the index, which is the durable copy, as a
    ``geometry_only`` document -- the appearance pass, if one ran, is the only
    thing lost, and ``enrich_rooms`` can run it again.
    """
    if not home_id or not room_key:
        return None
    from .. import room_context

    document = room_context.load(home_id, room_key)
    if document is not None:
        return document
    index = load_index(home_id)
    room = index.by_key(room_key) if index else None
    if room is None:
        return None
    # No local document, but the appearance pass ran before the upload and
    # its answer travelled with the index. This is the normal case on a
    # deployed server, which never sees the export.
    if room.appearance:
        return dict(room.appearance)
    if not room.measurements:
        return None
    return {
        "room_key": room.key,
        "room": "",
        "objects": [
            {"class": label, "appearance": "", "certainty": "unobserved"}
            for label in sorted(room.objects)
        ],
        "surfaces": {},
        "style": "",
        "notable": [],
        "measurements": dict(room.measurements),
        "coverage": "geometry_only",
        "coverage_reason": "rebuilt from the home index",
        "frames": [],
    }


def list_home_ids() -> list[str]:
    """Every home with an index on this host's disk (memory and local file).
    The durable copies are merged in by ``list_home_ids_async``."""
    ids = set(_cache)
    base = Path(settings.storage_dir) / "homes"
    if base.is_dir():
        for path in base.glob("*.json"):
            ids.add(path.stem)
    return sorted(ids)


async def list_home_ids_async() -> list[str]:
    """Local plus durable (Supabase ``home-indexes/``), so an ops listing on a
    fresh instance still shows every ingested home."""
    from . import supabase_store

    durable = set(await supabase_store.list_home_indexes() or [])
    # Local file stems are sanitised (_safe); a durable id that sanitises to
    # the same stem is the same home, listed once under its real id.
    sanitised = {_safe(h) for h in durable}
    ids = set(durable)
    ids.update(h for h in list_home_ids() if h not in sanitised)
    return sorted(ids)


def forget(home_id: str) -> None:
    """Drop an index (homeowner deletion path, SOW section 12). The durable
    copy goes too, or deletion would only last until the next rehydrate."""
    known_index = load_index(home_id)
    _cache.pop(home_id, None)
    path = _path(home_id)
    if path.exists():
        path.unlink()
    # The room contexts describe the home in words, so leaving them behind would
    # make this a partial deletion rather than a deletion.
    from .. import room_context

    removed = room_context.forget(home_id)
    if removed:
        logger.info("Removed %d room contexts for home %s", removed, home_id)
    from . import supabase_store

    if not supabase_store.enabled():
        return

    async def _delete_all() -> None:
        payload = known_index.to_json() if known_index else await supabase_store.get_home_index(home_id)
        owner = (payload or {}).get("upload", {}).get("ownerId")
        if owner:
            await supabase_store.delete_scan_uploads(home_id, owner)
        await supabase_store.delete_home_index(home_id)
        # The baked models describe the home in pixels; they go too.
        await supabase_store.delete_home_models(home_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_delete_all())
        return
    task = loop.create_task(_delete_all())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def _cli() -> None:  # pragma: no cover -- operator convenience
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(
        prog="python -m app.flow.home_registry",
        description="Ingest a TakeShape whole-home export into a named home index.",
    )
    parser.add_argument("bundle_dir", help="unzipped export directory (has meta.json and rooms/)")
    parser.add_argument("home_id", nargs="?", default=None,
                        help="defaults to the scan id in meta.json")
    parser.add_argument("--enrich", metavar="ROOMS", default="all",
                        help="the appearance pass, on by default: 'all' or a comma list of "
                             "room keys (one model call per room; needs LIDARAI_ANTHROPIC_API_KEY)")
    parser.add_argument("--no-enrich", action="store_true",
                        help="skip the appearance pass; the agent will know room shapes "
                             "but not floors, walls or finishes")
    parser.add_argument("--refresh", action="store_true",
                        help="with --enrich: rebuild even when a document is cached")
    args = parser.parse_args()

    index = ingest_bundle(args.bundle_dir, args.home_id)
    home_id = args.home_id or index.bundle_id
    ov = index.overview()
    print(f"ingested {home_id}: {ov['roomCount']} rooms, {ov['totalAreaSqFt']} sq ft, "
          f"{ov['namedConfidently']} confidently named -> {_path(home_id)}")
    for room in sorted(index.rooms, key=lambda r: (r.storey, -r.area_sqft)):
        measurements = room.measurements or {}
        paintable = measurements.get("paintable_sqft")
        model = room.model or {}
        if model.get("object"):
            model_note = f"model {model['bytes'] / 1048576:.0f} MB stored"
        elif model.get("skipped"):
            model_note = f"model {model['bytes'] / 1048576:.0f} MB NOT stored ({model['skipped']})"
        elif model:
            model_note = f"model {model['bytes'] / 1048576:.0f} MB"
        else:
            model_note = "no model in export"
        print(
            f"  {room.display_name:<26} [{room.key}] "
            f"floor {measurements.get('floor_sqft', 0):>6.0f} sqft  "
            f"paintable {paintable if paintable is not None else '--':>7} sqft  {model_note}"
        )

    if args.no_enrich:
        print("materials: skipped (--no-enrich). The agent will know room shapes only.")
    elif not settings.anthropic_api_key:
        print("WARNING: LIDARAI_ANTHROPIC_API_KEY is not set, so this home was ingested "
              "WITHOUT materials. The agent will know room shapes but not floors, walls "
              "or finishes. Set the key and run this again.")
    else:
        keys = None if args.enrich.strip().lower() == "all" else [
            k.strip() for k in args.enrich.split(",") if k.strip()
        ]
        documents = asyncio.run(
            enrich_rooms(args.bundle_dir, home_id, keys, refresh=args.refresh)
        )
        for key, document in documents.items():
            room = index.by_key(key)
            name = room.display_name if room else key
            print(f"  enriched {name:<24} [{key}] coverage={document.get('coverage')} "
                  f"room={document.get('room') or '-'} "
                  f"objects={len(document.get('objects') or [])}")
        enriched = sum(1 for d in documents.values() if d.get("coverage") != "geometry_only")
        print(f"materials: {enriched} of {len(documents)} rooms")
        if enriched < len(documents):
            print("WARNING: rooms at coverage=geometry_only have no materials; "
                  "see the log for why, then run again with --refresh.")


if __name__ == "__main__":  # pragma: no cover
    _cli()


async def save_index_confirmed(home_id: str, index: HomeIndex) -> None:
    """Do not acknowledge a staged upload before the durable index is saved."""
    from . import supabase_store

    # Older synchronous ingestion helpers may have scheduled snapshots.
    # Finish them before publishing this revision's final snapshot.
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
    if supabase_store.enabled() and not await supabase_store.put_home_index(home_id, index.to_json()):
        raise RuntimeError("Could not save the home index to durable storage; retry this upload")
    save_index(home_id, index, durable=False)
