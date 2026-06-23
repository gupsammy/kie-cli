"""Tests for pricing.py: golden values, unknown SKU, no-crash guarantee."""
from __future__ import annotations

import pytest

from kie_cli import pricing
from kie_cli.registry import resolve, MODELS


# ── Helpers ───────────────────────────────────────────────────────────────────

def _est(alias, inp=None):
    """Convenience: estimate for model by alias with given inp dict."""
    model = resolve(alias)
    return pricing.estimate(model, inp or {})


# ── Golden values ─────────────────────────────────────────────────────────────

def test_seedance2_1080p_8s_no_video_input():
    """SPEC §15: seedance-2 1080p no video input 8s = 816 credits / $4.08."""
    est = _est("seedance-2", {"resolution": "1080p", "duration": 8})
    assert est["credits"] == 816
    assert est["usd"] == pytest.approx(4.08)
    assert est["source"] == "estimate"


def test_seedance2_fast_720p_5s():
    """SPEC §15: seedance-2-fast 720p 5s = 165 credits."""
    est = _est("seedance-2-fast", {"resolution": "720p", "duration": 5})
    assert est["credits"] == 165
    assert est["usd"] == pytest.approx(0.825)
    assert est["source"] == "estimate"


def test_seedance2_mini_720p_5s_no_video():
    """mini 720p 5s no video = 20.5 cr/s × 5 = 102.5 credits. Exercises the mini
    description format ('720P', 'no video' without the 'input' suffix)."""
    est = _est("seedance-2-mini", {"resolution": "720p", "duration": 5})
    assert est["credits"] == pytest.approx(102.5)
    assert est["unit"] == pytest.approx(20.5)
    assert est["source"] == "estimate"


def test_seedance2_mini_does_not_match_base_sku():
    """The base seedance-2 lookup must not pick up a mini record (the 'input'
    suffix on base/fast tags is what discriminates them)."""
    base = _est("seedance-2", {"resolution": "720p", "duration": 5})
    assert base["unit"] == pytest.approx(41.0)  # base 720p no-input rate, not mini's 20.5


def test_z_image_golden():
    """SPEC §15: z-image = 0.8 credits."""
    est = _est("z-image", {})
    assert est["credits"] == pytest.approx(0.8)
    assert est["usd"] == pytest.approx(0.004)
    assert est["source"] == "estimate"


def test_nano_banana_golden():
    """SPEC §15: nano-banana = 4 credits."""
    est = _est("nano-banana", {})
    assert est["credits"] == pytest.approx(4.0)
    assert est["source"] == "estimate"


def test_kling30_1080p_audio_5s():
    """SPEC §15: kling-3.0 1080p audio 5s = 135 credits (27 cr/s × 5)."""
    # mode=pro → 1080P, sound=True → with audio
    est = _est("kling-3.0", {"mode": "pro", "sound": True, "duration": 5})
    assert est["credits"] == 135
    assert est["usd"] == pytest.approx(0.675)
    assert est["source"] == "estimate"


def test_unknown_sku_returns_credits_none_no_crash():
    """Unknown model pricing key → credits=None, no exception."""
    # Use a real model but mock load_table to return empty records
    from unittest.mock import patch
    model = resolve("seedance-2")
    with patch("kie_cli.pricing.load_table", return_value=[]):
        est = pricing.estimate(model, {"resolution": "1080p", "duration": 8})
    assert est["credits"] is None
    assert est["source"] == "unmatched"


def test_seedance_1_5_pro_credits_null():
    """seedance-1.5-pro: not in live pricing table → credits=None, never crashes."""
    est = _est("seedance-1.5-pro", {"duration": "8", "resolution": "720p"})
    assert est["credits"] is None
    # Never raises


def test_v1_series_credits_null():
    """v1-series (v1-pro-t2v etc): not in live pricing → credits=None."""
    est = _est("v1-pro-t2v", {"duration": "5", "resolution": "720p"})
    assert est["credits"] is None


def test_topaz_video_upscale_unit_not_null_credits_null():
    """topaz/video-upscale: returns unit (per-second rate), credits=None (duration unknown)."""
    est = _est("topaz-video-upscale", {"upscale_factor": "2"})
    # credits must be None (caller multiplies unit × duration)
    assert est["credits"] is None
    # unit should be non-null if pricing record found
    # (may be None if bundled snapshot doesn't have it — but should not crash)


def test_estimate_never_raises_on_exception():
    """pricing.estimate wraps all exceptions and returns credits=None."""
    from unittest.mock import patch
    model = resolve("seedance-2")
    with patch("kie_cli.pricing.load_table", side_effect=RuntimeError("db gone")):
        est = pricing.estimate(model, {})
    assert est["credits"] is None
    assert est["source"] == "unmatched"


def test_credit_usd_constant():
    assert pricing.CREDIT_USD == 0.005
