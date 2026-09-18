"""Tests for web-grounded price research: clamping, caching, and the
guidance integration (research itself is mocked — no live web calls)."""

import time

import pytest

from app.config import settings
from app.flow.price_research import (
    JobEstimate,
    ResearchedRates,
    _clamp,
    _read_cache,
    _write_cache,
)
from app.flow.pricing import (
    compute_price_guidance,
    parse_size_hint,
    static_rates,
    typical_job_band,
    user_asked_for_price,
)
from app.home_guide_tools import KNOWN_SERVICE_TYPES


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    yield


def rates(low, high, label="San Francisco metro"):
    return ResearchedRates(low, high, label, time.time())


class TestClamp:
    def test_reasonable_rates_pass(self):
        low, high = static_rates("Flooring")  # 7.0 / 18.0
        out = _clamp(rates(9.0, 22.0), low, high)
        assert out.low_per_sqft == 9.0 and out.high_per_sqft == 22.0

    def test_absurd_rates_bounded(self):
        low, high = static_rates("Flooring")
        out = _clamp(rates(0.5, 500.0), low, high)
        assert out.low_per_sqft == pytest.approx(7.0 * 0.25)
        assert out.high_per_sqft == pytest.approx(18.0 * 4)

    def test_garbage_rejected(self):
        low, high = static_rates("Flooring")
        assert _clamp(rates(-1, 5), low, high) is None
        assert _clamp(rates(10, 5), low, high) is None

    def test_narrow_range_widened(self):
        low, high = static_rates("Flooring")
        out = _clamp(rates(10.0, 10.5), low, high)
        assert out.high_per_sqft == pytest.approx(14.0)  # low * 1.4


class TestCache:
    def test_round_trip(self):
        _write_cache("Flooring", "94116", rates(9, 22))
        cached = _read_cache("Flooring", "94116")
        assert cached.low_per_sqft == 9 and cached.region_label == "San Francisco metro"

    def test_expired_cache_ignored(self):
        stale = ResearchedRates(9, 22, "x", time.time() - 40 * 24 * 3600)
        _write_cache("Flooring", "94116", stale)
        assert _read_cache("Flooring", "94116") is None


class TestGuidanceIntegration:
    def test_researched_rates_change_basis_and_numbers(self):
        static = compute_price_guidance("Flooring", 567)
        regional = compute_price_guidance("Flooring", 567, researched=rates(9.0, 22.0))
        assert "San Francisco metro" in regional.basis
        assert "web sources" in regional.basis
        assert regional.lowUsd != static.lowUsd
        assert regional.highUsd > regional.lowUsd
        assert regional.disclaimer  # unconditional

    def test_static_fallback_unchanged_shape(self):
        g = compute_price_guidance("Flooring", 567)
        assert "approximate bounding measurements" in g.basis
        assert g.lowUsd > 0 and g.highUsd > g.lowUsd


class TestJobEstimate:
    def _je(self, low, high, region="Testville"):
        from app.flow.price_research import JobEstimate
        import time as _t
        return JobEstimate(low, high, region, _t.time())

    def test_job_estimate_is_tight_and_named(self):
        je = self._je(1500, 3000, "Dayton, OH")
        g = compute_price_guidance("Painting", 508, job_estimate=je)
        assert "Dayton, OH" in g.basis and "estimated for" in g.basis
        # Tighter than the wide static fallback for the same job.
        wide = compute_price_guidance("Painting", 508)
        assert (g.highUsd - g.lowUsd) < (wide.highUsd - wide.lowUsd)

    def test_clamp_bounds_absurd_total(self):
        from app.flow.price_research import _clamp_job
        from app.flow.pricing import static_rates
        lo_rate, hi_rate = static_rates("Painting")
        # A wildly high total gets bounded to <= static_high_rate * area * 3.
        out = _clamp_job(self._je(50, 999999), 500, lo_rate, hi_rate)
        assert out.high_usd <= hi_rate * 500 * 3
        assert out.high_usd >= out.low_usd * 1.2

    def test_clamp_rejects_garbage(self):
        from app.flow.price_research import _clamp_job
        assert _clamp_job(self._je(-1, 5), 500, 2.5, 6.5) is None
        assert _clamp_job(self._je(3000, 1000), 500, 2.5, 6.5) is None

    def test_clamp_caps_spread(self):
        from app.flow.price_research import _clamp_job
        out = _clamp_job(self._je(1000, 9000), 500, 2.5, 6.5)  # 9x spread
        assert out.high_usd <= out.low_usd * 4  # capped at 4x


# -------------------------------------- sizeless bands (pre-scan, Sep 12 2026)
class TestSizelessGuidance:
    """The common case: a homeowner asks what something costs in their zip
    before anything has been measured. A wide honest band beats a refusal."""

    def test_every_service_has_a_band_without_measurements(self):
        for service in KNOWN_SERVICE_TYPES:
            g = compute_price_guidance(service, None)
            assert g is not None, service
            assert g.highUsd > g.lowUsd > 0, service
            assert g.disclaimer, service
            assert "nothing measured yet" in g.basis, service

    def test_sizeless_band_is_wider_than_a_measured_one(self):
        loose = compute_price_guidance("Painting", None)
        tight = compute_price_guidance("Painting", 400)
        assert (loose.highUsd / loose.lowUsd) > (tight.highUsd / tight.lowUsd)

    def test_no_service_still_returns_nothing(self):
        assert compute_price_guidance(None, None) is None
        assert compute_price_guidance("", 500) is None

    def test_non_sqft_trades_are_not_priced_off_floor_area(self):
        """A mover charges by truck-hours; interior floor area says nothing.
        These trades must fall to the typical-job band, not the sqft default."""
        for service in ("Moving", "Junk Removal", "Handyman", "Gutter Cleaning"):
            low, high = typical_job_band(service)
            assert (low, high) != typical_job_band("__missing__"), service

    def test_clamp_bounds_absurd_total_without_area(self):
        from app.flow.price_research import _clamp_job

        je = JobEstimate(50, 999999, "Nashville", time.time())
        out = _clamp_job(je, None, 2.5, 6.5, service="Junk Removal")
        typical_high = typical_job_band("Junk Removal")[1]
        assert out.high_usd <= typical_high * 3
        assert out.high_usd >= out.low_usd * 1.2


class TestSizeHint:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("it's about 1800 sq ft", 1800.0),
            ("roughly 2,000 square feet", 2000.0),
            ("1200 sqft maybe", 1200.0),
            ("3 bedrooms", 540.0),
            ("just one room", 180.0),
            ("the whole house", 1800.0),
        ],
    )
    def test_parses_volunteered_sizes(self, message, expected):
        assert parse_size_hint(message) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "message",
        [
            None,
            "",
            "how much does painting cost?",
            "I live in 37212",
            "18000000 sq ft",   # typo'd, out of bounds
            "0.5 sq ft",        # out of bounds
            "40 bedrooms",      # out of bounds
        ],
    )
    def test_returns_none_rather_than_guessing(self, message):
        assert parse_size_hint(message) is None


# ------------------------------- the runtime path, end to end (no web calls)
class TestPreScanQuotePath:
    """The change that matters: a ballpark before any scan or contractor.
    Research is off here, so this exercises the static/typical fallback."""

    @pytest.fixture(autouse=True)
    def no_research(self, monkeypatch):
        monkeypatch.setattr(settings, "agent_price_guidance_enabled", True)
        monkeypatch.setattr(settings, "price_research_enabled", False)

    def _turn(self, message, project_type="Painting", zip_code="37212"):
        import asyncio

        from app.flow import FlowState
        from app.flow_runtime import _maybe_price_guidance
        from app.home_ai import HomeAIChatRequest

        state = FlowState()
        state.slots.project_type = project_type
        state.slots.zip = zip_code
        request = HomeAIChatRequest(threadId="t-price", message=message)
        return state, asyncio.run(_maybe_price_guidance(state, request))[0]

    def test_price_ask_with_no_scan_still_returns_a_band(self):
        _, guidance = self._turn("roughly how much would this cost?")
        assert guidance is not None
        assert guidance.highUsd > guidance.lowUsd > 0
        assert "nothing measured yet" in guidance.basis
        assert guidance.disclaimer

    def test_a_volunteered_size_narrows_the_band(self):
        _, loose = self._turn("how much would this cost?")
        _, tight = self._turn("how much for about 400 sq ft?")
        assert "nothing measured yet" not in tight.basis
        assert (tight.highUsd / tight.lowUsd) < (loose.highUsd / loose.lowUsd)

    def test_the_band_is_pinned_across_turns_but_repriced_on_a_new_size(self):
        from app.flow_runtime import _maybe_price_guidance
        from app.home_ai import HomeAIChatRequest
        import asyncio

        state, first = self._turn("what does this cost?")
        again = asyncio.run(
            _maybe_price_guidance(
                state, HomeAIChatRequest(threadId="t-price", message="and the price again?")
            )
        )[0]
        assert (again.lowUsd, again.highUsd) == (first.lowUsd, first.highUsd)
        sized = asyncio.run(
            _maybe_price_guidance(
                state, HomeAIChatRequest(threadId="t-price", message="cost for 400 sq ft?")
            )
        )[0]
        assert (sized.lowUsd, sized.highUsd) != (first.lowUsd, first.highUsd)

    def test_a_message_that_is_not_a_price_ask_gets_the_band_but_not_the_pitch(self):
        """The band is built whenever the trade is known, so the model can
        answer a cost question the regex did not recognise. `asked` stays
        False, which is what keeps the search unspent and the directive
        conditional."""
        from app.flow_runtime import _maybe_price_guidance
        from app.home_ai import HomeAIChatRequest
        import asyncio

        from app.flow import FlowState

        state = FlowState()
        state.slots.project_type = "Painting"
        state.slots.zip = "37212"
        guidance, asked = asyncio.run(
            _maybe_price_guidance(
                state,
                HomeAIChatRequest(threadId="t-price", message="what colour would you use in here?"),
            )
        )
        assert guidance is not None
        assert asked is False
        # An unasked-for band is never pinned: the real question, when it
        # comes, still gets to run the search.
        assert state.price_guidance_snapshot is None


class TestPriceAskDetection:
    """Found live: "what DO maid services run" was missed while "what WOULD
    a repaint run" matched, so a perfectly normal ask got no card at all."""

    @pytest.mark.parametrize(
        "message",
        [
            "what do maid services run in 90210?",
            "what would a repaint run?",
            "what does junk removal run?",
            "what will movers charge?",
            "how much is gutter cleaning?",
            "roughly what does that cost",
            "give me a price range",
            # Follow-ups after a card was already shown — refusing these
            # right after handing over a range reads broken.
            "what is the hourly rate painters charge there?",
            "break that down per square foot",
            "whats the rate",
            "what are their rates",
            # Live Sep 13: refused with no card.
            "and are you sure you can't give me a general guess for the quote?",
            "just your best guess?",
            "rough idea what it would run?",
            "a ball park number",
        ],
    )
    def test_recognises_a_price_ask(self, message):
        assert user_asked_for_price(message)

    @pytest.mark.parametrize(
        "message",
        [
            "what colour would you use in here?",
            "what do you think of the layout?",
            "can you run me through the options?",
            "what would you do with this room?",
            "how long does it take?",
            # "rate" as a verb is not a price ask.
            "can you rate this design?",
            "how would you rate that finish",
            "break down the steps for me",
            "I guess I'd go with the lighter grey",
        ],
    )
    def test_does_not_fire_on_ordinary_design_talk(self, message):
        assert not user_asked_for_price(message)
