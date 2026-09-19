"""Rank a local provider candidate set by review and social presence.

Pure arithmetic -- no I/O, no settings read at import time. The candidate
rows come from ``partners`` (the durable provider table, with the
``presences`` list that discovery attaches) plus any web-researched leads
for the same trade and zip; the weights come from ``RankingWeights``, which
is built from configuration so every term can be tuned without a code
change. Design record: ``docs/adr/provider-ranking.md``.

Why not sum raw counts across platforms: a Google review count and an
Instagram follower count are different units on different scales, so a
raw sum ranks by whichever platform inflates numbers most. Instead:

* each platform is scored on its own, within the local candidate set --
  the percentile rank of ``log1p(volume)`` blended with an absolute
  log-scaled term so a set of three tiny businesses does not mint a
  "top" provider out of eight reviews;
* the rating is shrunk toward a prior by review volume, so five stars on
  two reviews does not beat 4.6 on 150;
* platform scores combine as a weighted mean over the platforms that
  actually have data -- a missing platform is missing, never a zero;
* a modest bonus rewards being present on more than one platform;
* a provider that has actually quoted through this system gets an
  explicit, separately named boost so it can be tuned or switched off.

Every candidate comes back with its per-platform components so a ranking
can be explained, audited, and debugged from the ops email alone.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("lidarai.flow.provider_ranking")

PLATFORMS = ("google", "yelp", "facebook", "instagram", "nextdoor")

# Relationship precedence for tie-breaks (and for the deprecated
# partner-first ordering): a row's own relationship first, then researched
# leads that are not in the table at all.
_RELATIONSHIP_ORDER = {"partner": 0, "quoted": 1, "prospect": 2, "researched": 3}


def parse_platform_weights(text: str) -> dict[str, float]:
    """``"google=1.0,yelp=0.6"`` -> ``{"google": 1.0, "yelp": 0.6}``.
    Unknown platforms are kept (a new platform is configuration); bad
    entries are skipped with a warning rather than failing the ranking."""
    weights: dict[str, float] = {}
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition("=")
        try:
            weights[name.strip().lower()] = float(value)
        except ValueError:
            logger.warning("Ignoring platform weight %r", part)
    return weights


def _default_platform_weights() -> dict[str, float]:
    return {"google": 1.0, "yelp": 0.6, "facebook": 0.4, "instagram": 0.4, "nextdoor": 0.4}


@dataclass(frozen=True)
class RankingWeights:
    platform_weights: dict[str, float] = field(default_factory=_default_platform_weights)
    volume_weight: float = 0.6
    rating_weight: float = 0.4
    volume_percentile_weight: float = 0.6
    volume_log_weight: float = 0.4
    volume_reference_count: int = 200
    rating_prior: float = 4.0
    rating_shrinkage_count: int = 10
    multi_platform_bonus: float = 0.05
    multi_platform_bonus_cap: float = 0.15
    quoted_boost: float = 0.25
    quoted_boost_saturation: int = 5
    no_data_baseline: float = 0.0

    @classmethod
    def from_settings(cls, s: Any = None) -> "RankingWeights":
        if s is None:
            from ..config import settings as s  # noqa: PLC0415
        return cls(
            platform_weights=parse_platform_weights(s.rank_platform_weights) or _default_platform_weights(),
            volume_weight=float(s.rank_volume_weight),
            rating_weight=float(s.rank_rating_weight),
            volume_percentile_weight=float(s.rank_volume_percentile_weight),
            volume_log_weight=float(s.rank_volume_log_weight),
            volume_reference_count=int(s.rank_volume_reference_count),
            rating_prior=float(s.rank_rating_prior),
            rating_shrinkage_count=int(s.rank_rating_shrinkage_count),
            multi_platform_bonus=float(s.rank_multi_platform_bonus),
            multi_platform_bonus_cap=float(s.rank_multi_platform_bonus_cap),
            quoted_boost=float(s.rank_quoted_boost),
            quoted_boost_saturation=int(s.rank_quoted_boost_saturation),
            no_data_baseline=float(s.rank_no_data_baseline),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "platformWeights": dict(self.platform_weights),
            "volumeWeight": self.volume_weight,
            "ratingWeight": self.rating_weight,
            "volumePercentileWeight": self.volume_percentile_weight,
            "volumeLogWeight": self.volume_log_weight,
            "volumeReferenceCount": self.volume_reference_count,
            "ratingPrior": self.rating_prior,
            "ratingShrinkageCount": self.rating_shrinkage_count,
            "multiPlatformBonus": self.multi_platform_bonus,
            "multiPlatformBonusCap": self.multi_platform_bonus_cap,
            "quotedBoost": self.quoted_boost,
            "quotedBoostSaturation": self.quoted_boost_saturation,
            "noDataBaseline": self.no_data_baseline,
        }


# --------------------------------------------------------------- primitives
def _number(value: Any) -> float | None:
    """A stored metric, or None. Zero is a real value ("0 reviews" from an
    API that said so); absence stays absence."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0 else None


def presence_volume(presence: dict[str, Any]) -> float | None:
    """Review count when the platform has one, else follower count. A
    platform that reported neither has no volume -- it is not "0"."""
    count = _number(presence.get("reviewCount"))
    if count is not None:
        return count
    return _number(presence.get("followerCount"))


def percentile_rank(value: float, population: list[float]) -> float:
    """Mid-rank percentile in (0, 1): a lone value sits at 0.5, two distinct
    values at 0.25 / 0.75, ties share a rank. Never 0 or 1 for a real value,
    so the smallest local set does not mint an extreme."""
    if not population:
        return 0.5
    below = sum(1 for v in population if v < value)
    equal = sum(1 for v in population if v == value)
    return (below + 0.5 * max(equal, 1)) / len(population)


def log_scaled(value: float, reference: int) -> float:
    """``log1p(v) / log1p(reference)`` capped at 1 -- an absolute sense of
    volume so percentile alone cannot promote eight reviews to the top."""
    if reference <= 0:
        return 1.0
    return min(1.0, math.log1p(max(value, 0.0)) / math.log1p(reference))


def shrunk_rating(rating: float, volume: float | None, w: RankingWeights) -> float:
    """Rating on 0..1, pulled toward the prior by how few reviews back it.
    ``volume`` None (a rating with no count) shrinks all the way to the
    prior: an unsupported rating is no evidence either way."""
    n = volume if volume is not None else 0.0
    k = max(float(w.rating_shrinkage_count), 0.0)
    trust = n / (n + k) if (n + k) > 0 else 1.0
    return trust * (rating / 5.0) + (1.0 - trust) * (w.rating_prior / 5.0)


# ------------------------------------------------------------------ scoring
def _presences_by_platform(candidate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for presence in candidate.get("presences") or []:
        if not isinstance(presence, dict):
            continue
        platform = str(presence.get("platform") or "").strip().lower()
        if platform:
            out[platform] = presence
    return out


def _quoted_boost(candidate: dict[str, Any], w: RankingWeights) -> float:
    count = _number(candidate.get("quotedCount")) or 0.0
    if count <= 0 or w.quoted_boost <= 0:
        return 0.0
    saturation = max(w.quoted_boost_saturation, 1)
    return w.quoted_boost * min(1.0, math.log1p(count) / math.log1p(saturation))


def _round(value: Any) -> Any:
    return round(value, 4) if isinstance(value, float) else value


def rank_candidates(
    candidates: list[dict[str, Any]],
    weights: RankingWeights | None = None,
    *,
    legacy_preferred_ordering: bool = False,
) -> list[dict[str, Any]]:
    """Score and order a candidate set for one trade + area.

    Each candidate is a provider row (``name``, ``relationship``,
    ``presences``, ``quotedCount``, contact fields...). Returns one entry
    per candidate, best first::

        {"rank": 1, "name": ..., "relationship": ..., "score": 0.81,
         "components": {"platforms": {"google": {...}}, "presenceScore": ...,
                        "multiPlatformBonus": ..., "quotedBoost": ...,
                        "baseline": ..., "dataCoverage": "full|partial|links_only|none",
                        "smallSet": bool},
         "row": <the candidate as given>}

    ``legacy_preferred_ordering`` is the DEPRECATED partner-first path:
    relationship decides the order and the score only breaks ties. It maps
    onto the same note_quoted promotion the ``quotedBoost`` term carries.
    """
    w = weights or RankingWeights()
    rows = [c for c in candidates if isinstance(c, dict) and str(c.get("name") or "").strip()]
    small_set = len(rows) < 3

    # Per-platform populations of log-volume, over candidates that have a
    # volume on that platform -- the percentile is within the local set.
    populations: dict[str, list[float]] = {}
    volumes: list[dict[str, float | None]] = []
    for row in rows:
        per: dict[str, float | None] = {}
        for platform, presence in _presences_by_platform(row).items():
            vol = presence_volume(presence)
            per[platform] = vol
            if vol is not None:
                populations.setdefault(platform, []).append(math.log1p(vol))
        volumes.append(per)

    results: list[dict[str, Any]] = []
    for row, per_volume in zip(rows, volumes):
        presences = _presences_by_platform(row)
        platform_components: dict[str, dict[str, Any]] = {}
        weighted_sum = 0.0
        weight_total = 0.0
        for platform, presence in presences.items():
            volume = per_volume.get(platform)
            rating = _number(presence.get("rating"))
            if rating is not None and rating > 5.0:
                rating = None  # not a 5-point scale; do not guess
            component: dict[str, Any] = {
                "rating": rating,
                "reviewCount": _number(presence.get("reviewCount")),
                "followerCount": _number(presence.get("followerCount")),
                "volume": volume,
                "profileUrl": presence.get("profileUrl"),
                "source": presence.get("source"),
                "lastVerifiedAt": presence.get("lastVerifiedAt"),
                "volumeScore": None,
                "ratingScore": None,
                "score": None,
                "weight": float(w.platform_weights.get(platform, 0.0)),
            }
            if volume is not None:
                pct = percentile_rank(math.log1p(volume), populations.get(platform, []))
                logv = log_scaled(volume, w.volume_reference_count)
                denom = w.volume_percentile_weight + w.volume_log_weight
                component["volumePercentile"] = pct
                component["volumeLog"] = logv
                component["volumeScore"] = (
                    (w.volume_percentile_weight * pct + w.volume_log_weight * logv) / denom
                    if denom > 0
                    else pct
                )
            if rating is not None:
                component["ratingScore"] = shrunk_rating(rating, volume, w)

            vs, rs = component["volumeScore"], component["ratingScore"]
            if vs is not None and rs is not None:
                denom = w.volume_weight + w.rating_weight
                component["score"] = (w.volume_weight * vs + w.rating_weight * rs) / denom if denom > 0 else vs
            elif vs is not None:
                component["score"] = vs
            elif rs is not None:
                component["score"] = rs
            # A presence with a profile link but no numbers is recorded (it
            # is a real, auditable link) but contributes no score.
            platform_components[platform] = component
            if component["score"] is not None and component["weight"] > 0:
                weighted_sum += component["weight"] * component["score"]
                weight_total += component["weight"]

        scored_platforms = [p for p, c in platform_components.items() if c["score"] is not None]
        presence_score = weighted_sum / weight_total if weight_total > 0 else None
        multi_bonus = 0.0
        if len(scored_platforms) >= 2:
            multi_bonus = min(w.multi_platform_bonus_cap, w.multi_platform_bonus * (len(scored_platforms) - 1))
        quoted_boost = _quoted_boost(row, w)
        baseline = w.no_data_baseline if presence_score is None else 0.0
        score = (presence_score if presence_score is not None else w.no_data_baseline) + multi_bonus + quoted_boost

        if not platform_components:
            coverage = "none"
        elif scored_platforms:
            coverage = "full" if len(scored_platforms) == len(platform_components) else "partial"
        else:
            coverage = "links_only"

        relationship = str(row.get("relationship") or "prospect").lower()
        results.append(
            {
                "name": str(row.get("name")),
                "relationship": relationship,
                "score": round(score, 4),
                "components": {
                    "platforms": {
                        p: {k: _round(v) for k, v in c.items()} for p, c in platform_components.items()
                    },
                    "presenceScore": None if presence_score is None else round(presence_score, 4),
                    "scoredPlatforms": scored_platforms,
                    "multiPlatformBonus": round(multi_bonus, 4),
                    "quotedBoost": round(quoted_boost, 4),
                    "quotedCount": int(_number(row.get("quotedCount")) or 0),
                    "baseline": baseline,
                    "dataCoverage": coverage,
                    "smallSet": small_set,
                },
                "row": row,
            }
        )

    def default_key(entry: dict[str, Any]) -> tuple:
        return (
            -entry["score"],
            _RELATIONSHIP_ORDER.get(entry["relationship"], 9),
            entry["name"].lower(),
        )

    def legacy_key(entry: dict[str, Any]) -> tuple:
        return (
            _RELATIONSHIP_ORDER.get(entry["relationship"], 9),
            -entry["components"]["quotedCount"],
            -entry["score"],
            entry["name"].lower(),
        )

    results.sort(key=legacy_key if legacy_preferred_ordering else default_key)
    for i, entry in enumerate(results, start=1):
        entry["rank"] = i
    return results


def explain(entry: dict[str, Any]) -> str:
    """One line per ranked entry for logs and the ops email: every term
    that moved the score, with the platform numbers behind it."""
    comps = entry.get("components") or {}
    bits: list[str] = []
    for platform, c in (comps.get("platforms") or {}).items():
        if c.get("score") is None:
            bits.append(f"{platform}: profile link only")
            continue
        detail = []
        if c.get("rating") is not None:
            detail.append(f"{c['rating']:.1f} stars")
        if c.get("reviewCount") is not None:
            detail.append(f"{int(c['reviewCount'])} reviews")
        elif c.get("followerCount") is not None:
            detail.append(f"{int(c['followerCount'])} followers")
        detail.append(f"score {c['score']:.2f}")
        if c.get("lastVerifiedAt"):
            detail.append(f"verified {str(c['lastVerifiedAt'])[:10]}")
        bits.append(f"{platform}: " + " / ".join(detail))
    if comps.get("multiPlatformBonus"):
        bits.append(f"multi-platform +{comps['multiPlatformBonus']:.2f}")
    if comps.get("quotedBoost"):
        bits.append(f"quoted boost +{comps['quotedBoost']:.2f} ({comps.get('quotedCount', 0)}x)")
    if comps.get("dataCoverage") == "none":
        bits.append("no review or social data")
    if comps.get("smallSet"):
        bits.append("small local set (<3 candidates)")
    return "; ".join(bits)
