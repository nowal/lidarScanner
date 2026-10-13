"""Capture changes and bounded, grounded copy for assistant-only events."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..home_index import _point_in_polygon


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def source_fingerprint(bundle: Path, key: str) -> str:
    """Ignore ZIP metadata, generated descriptions and all textured models."""
    root = bundle / 'rooms' / key
    parts = {}
    for name in ('room.json', 'floor.json', 'live.json'):
        path = root / name
        if path.exists():
            raw = path.read_bytes()
            try:
                parts[name] = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                # Index ingestion already tolerates absent/unreadable geometry.
                # Fingerprinting must not turn that into a new ingestion error.
                parts[name] = {'unparsedSHA256': hashlib.sha256(raw).hexdigest()}
    manifest = root / 'rebuild/manifest.json'
    try:
        frames = json.loads(manifest.read_bytes()).get('frames', []) if manifest.exists() else []
    except (ValueError, UnicodeDecodeError, AttributeError):
        frames = []
    parts['frames'] = sorted(frames, key=lambda f: str(f.get('id', '')))
    parts['images'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted((root / 'rebuild/images').glob('*.jpg'))}
    return digest(parts)


def same_capture(before: dict, after: dict) -> bool:
    a, b = before.get('contextFingerprint'), after.get('contextFingerprint')
    if a and b:
        return a == b
    # Upgrade from an index made before fingerprints existed: compare actual
    # geometry/photo identities, never generated prose or inferred names.
    keys = ('polygon', 'floorY', 'objects', 'windows', 'windowOpenings',
            'doors', 'walls', 'frameIds', 'areaSqFt', 'measurements')
    return all(before.get(k) == after.get(k) for k in keys)


def overlaps(after: dict, before: dict) -> bool:
    """Conservative association of a new capture key with an existing area.

    Update Scan appends capture blocks. In the aligned world frame, at least
    80% of the new footprint must lie inside the old one on the same storey.
    Missing/ambiguous geometry stays unclassified; we never count new rooms.
    """
    a, b = after.get('polygon') or [], before.get('polygon') or []
    if (len(a) < 3 or len(b) < 3 or after.get('storey') != before.get('storey')
            or abs(after.get('floorY', 0) - before.get('floorY', 0)) > 1):
        return False
    xs, zs = [p[0] for p in a], [p[1] for p in a]
    if max(xs) == min(xs) or max(zs) == min(zs):
        return False
    points = [(min(xs) + (i + .5) / 16 * (max(xs) - min(xs)),
               min(zs) + (j + .5) / 16 * (max(zs) - min(zs)))
              for i in range(16) for j in range(16)]
    inside = [(x, z) for x, z in points if _point_in_polygon(x, z, a)]
    return bool(inside) and sum(_point_in_polygon(x, z, b) for x, z in inside) / len(inside) >= .8


def changes(before: dict, after: dict) -> list[dict]:
    old = {r['key']: r for r in before.get('rooms', [])}
    result = []
    for room in after.get('rooms', []):
        prior = old.get(room['key'])
        if prior and same_capture(prior, room):
            continue
        kind = 'refreshed' if prior else 'added'
        if prior is None:
            matches = [r for r in old.values() if overlaps(room, r)]
            if len(matches) == 1:
                prior, kind = matches[0], 'refreshed'
            elif matches or len(room.get('polygon') or []) < 3:
                kind = 'additional_views'
        # An explicit name on this area wins; an unambiguous rescan of a
        # homeowner-named area keeps their name without renaming the registry.
        named = room if room.get('namedByHomeowner') else prior if prior and prior.get('namedByHomeowner') else room
        certain = bool(named.get('namedByHomeowner') or named.get('confident'))
        label = (('' if named.get('namedByHomeowner') else 'the ') + str(named.get('name') or 'captured area')[:100]
                 if certain else 'an area I can’t confidently identify yet')
        result.append({'key': room['key'], 'kind': kind, 'label': label,
                       'matchedKey': prior['key'] if prior else None, 'room': room})
    return result


def copy_parts(delta: list[dict]) -> tuple[str, list[dict]]:
    phrases, observations = [], []
    for change in delta:
        label, kind, room = change['label'], change['kind'], change['room']
        verb = {'added': 'added coverage of', 'refreshed': 'refreshed the details for',
                'additional_views': 'shared additional views of'}[kind]
        phrase = f'{verb} {label}'
        if len(phrases) < 3 and phrase not in phrases:
            phrases.append(phrase)
        appearance = room.get('appearance') or {}
        if appearance.get('coverage') != 'geometry_only':
            for surface in ('walls', 'floor', 'ceiling', 'splashback'):
                detail = (appearance.get('surfaces') or {}).get(surface)
                if isinstance(detail, str) and detail.strip():
                    observations.append({'id': f'{room["key"]}:{surface}',
                        'text': f'In {label}, the {surface} {"appear" if surface == "walls" else "appears"} to be {detail.strip().rstrip(".")[:180]}.'})
        if not any(o['id'].startswith(room['key'] + ':') for o in observations):
            observations.append({'id': f'{room["key"]}:coverage',
                'text': f'I have updated scan geometry for {label}, but not enough clear photo detail to describe its finishes.'})
    intro = 'I have the updated scan details — you’ve ' + '; '.join(phrases) + '.'
    return intro, observations[:40]


def render(intro: str, observations: list[dict], selected: list[str]) -> str:
    # The model can choose useful evidence, never invent facts, ask the pending
    # flow question, or introduce a sales/quote action in an event response.
    available = {o['id']: o['text'] for o in observations}
    chosen = list(dict.fromkeys(k for k in selected if k in available))[:2]
    if not chosen and available:
        chosen = [next(iter(available))]
    return ' '.join([intro] + [available[k] for k in chosen])
