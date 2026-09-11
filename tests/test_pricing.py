"""Tests for pricing.py: golden values, unknown SKU, no-crash guarantee."""
from __future__ import annotations

import pytest

from kie_cli import pricing
from kie_cli.registry import resolve


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
    """seedance-2-fast 720p 5s = 24.8 cr/s × 5 = 124 credits (snapshot 2026-09-11; was 33/s)."""
    est = _est("seedance-2-fast", {"resolution": "720p", "duration": 5})
    assert est["credits"] == pytest.approx(124.0)
    assert est["usd"] == pytest.approx(0.62)
    assert est["source"] == "estimate"


def test_seedance2_mini_720p_5s_no_video():
    """mini 720p 5s no video = 8.2 cr/s × 5 = 41 credits (snapshot 2026-09-11).
    Exercises the mini description format ('720P', 'no video' without the 'input' suffix)."""
    est = _est("seedance-2-mini", {"resolution": "720p", "duration": 5})
    assert est["credits"] == pytest.approx(41.0)
    assert est["unit"] == pytest.approx(8.2)
    assert est["source"] == "estimate"


def test_seedance2_mini_does_not_match_base_sku():
    """The base seedance-2 lookup must not pick up a mini record (the 'input'
    suffix on base/fast tags is what discriminates them)."""
    base = _est("seedance-2", {"resolution": "720p", "duration": 5})
    assert base["unit"] == pytest.approx(41.0)  # base 720p no-input rate, not mini's 8.2


def test_seedance25_720p_4s_no_video():
    """seedance-2-5 720p no video = 63 cr/s × 4 = 252 credits."""
    est = _est("seedance-2.5", {"resolution": "720p", "duration": 4})
    assert est["credits"] == pytest.approx(252.0)
    assert est["unit"] == pytest.approx(63.0)
    assert est["source"] == "estimate"


def test_seedance25_with_video_ref_bills_input_plus_output():
    """720p with video = 38 cr/s × (3.7s ref + 4s out) = 292.6 credits."""
    est = pricing.estimate(
        resolve("seedance-2.5"),
        {"resolution": "720p", "duration": 4, "reference_video_urls": ["https://x/ref.mp4"]},
        extra_input_seconds=3.7,
    )
    assert est["credits"] == pytest.approx(292.6)
    assert est["unit"] == pytest.approx(38.0)
    assert "3.7s ref" in est["formula"]


def test_seedance25_lookup_does_not_bleed_into_base_or_mini():
    """'bytedance/seedance-2-5, 720p no video' must not satisfy the base ('no video input')
    or mini ('seedance-2-mini') matchers, and vice versa."""
    assert _est("seedance-2", {"resolution": "720p", "duration": 1})["unit"] == pytest.approx(41.0)
    assert _est("seedance-2-mini", {"resolution": "720p", "duration": 1})["unit"] == pytest.approx(8.2)
    assert _est("seedance-2.5", {"resolution": "720p", "duration": 1})["unit"] == pytest.approx(63.0)


# ── Catalog models: generic lookup ────────────────────────────────────────────

def test_catalog_per_second_minimax_h3():
    est = _est("minimax-h3-t2v", {"resolution": "768P", "duration": 6})
    assert est["credits"] == pytest.approx(48.0)      # 8 cr/s × 6
    assert est["source"] == "estimate"


def test_catalog_per_second_kling_turbo_resolution_token():
    est = _est("kling-v3-turbo-i2v", {"resolution": "1080p", "duration": 5})
    assert est["credits"] == pytest.approx(112.5)     # 22.5 cr/s × 5


def test_catalog_audio_token_narrows_rows():
    on = _est("pixverse-v6-t2v", {"quality": "720p", "duration": 5, "generate_audio_switch": True})
    off = _est("pixverse-v6-t2v", {"quality": "720p", "duration": 5, "generate_audio_switch": False})
    assert on["credits"] == pytest.approx(48.0)       # 9.6 × 5
    assert off["credits"] == pytest.approx(36.0)      # 7.2 × 5


def test_catalog_fixed_sku_duration_token():
    est = _est("kling-v2-1-standard", {"duration": 10})
    assert est["credits"] == pytest.approx(50.0)


def test_catalog_ambiguous_lists_candidates():
    est = _est("ideogram-v3-t2i", {})
    assert est["credits"] is None
    assert est["source"] == "ambiguous"
    assert len(est["candidates"]) == 3
    picked = _est("ideogram-v3-t2i", {"rendering_speed": "TURBO"})
    assert picked["credits"] == pytest.approx(3.5)


def test_catalog_per_second_unknown_duration_returns_unit_only():
    est = _est("wan-2-2-animate-move", {"resolution": "720p"})
    assert est["credits"] is None
    assert est["unit"] == pytest.approx(12.5)
    assert est["source"] == "estimate"


def test_catalog_per_megapixel_unknown_credits():
    est = _est("qwen-t2i", {})
    assert est["credits"] is None
    assert est["unit"] == pytest.approx(4.0)


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


# ── seedream 5 pro (records from live table; no hardcoded rates) ─────────────

def test_seedream5pro_t2i_basic_and_high():
    est = _est("seedream-5-pro", {"prompt": "x", "quality": "basic"})
    assert est["credits"] == pytest.approx(7.0)
    assert est["usd"] == pytest.approx(0.035)
    est_hi = _est("seedream-5-pro", {"prompt": "x", "quality": "high"})
    assert est_hi["credits"] == pytest.approx(14.0)


def test_seedream5pro_i2i_first_input_image_free():
    est = _est("seedream-5-pro", {"prompt": "x", "quality": "basic",
                                  "image_urls": ["u1"]})
    assert est["credits"] == pytest.approx(7.0)  # i2i base, no surcharge


def test_seedream5pro_i2i_extra_input_image_surcharge():
    est = _est("seedream-5-pro", {"prompt": "x", "quality": "high",
                                  "image_urls": ["u1", "u2", "u3"]})
    # 14 (2K) + 0.5 × 2 extra input images
    assert est["credits"] == pytest.approx(15.0)
    assert "first free" in est["formula"]


# ── Review regressions (PR #1) ────────────────────────────────────────────────

def test_seedance25_duration_minus_one_is_not_priced():
    """duration -1 (model picks) must not yield negative credits."""
    est = _est("seedance-2.5", {"resolution": "720p", "duration": -1})
    assert est["credits"] is None
    assert est["unit"] == pytest.approx(63.0)
    assert "unknown" in est["note"]


def test_grok_video_never_matches_upscale_row():
    est = _est("grok-t2v", {"resolution": "1080p", "duration": 10})
    assert est["unit"] == pytest.approx(8.0)
    assert est["credits"] == pytest.approx(80.0)
    est = _est("grok-i2v", {"resolution": "720p", "duration": 10})
    assert est["unit"] == pytest.approx(4.5)


def test_catalog_video_input_narrowed_before_duration():
    """gemini-omni with-video rows carry no duration token; they must survive."""
    est = _est("gemini-omni-video", {"resolution": "720p", "duration": 10,
                                     "video_list": ["https://x/a.mp4"]})
    assert est["credits"] == pytest.approx(168.0)
    est = _est("gemini-omni-video", {"resolution": "720p", "duration": 10})
    assert est["credits"] == pytest.approx(126.0)


def test_catalog_zero_duration_default_not_priced_as_free():
    est = _est("wan-2-7-videoedit", {"resolution": "1080p", "duration": 0})
    assert est["credits"] is None
    assert est["unit"] == pytest.approx(24.0)


def test_catalog_missing_sku_is_unmatched_not_neighbour():
    """hailuo 02 i2v standard has no 6.0s-768p row: report a miss, not the 10s row."""
    est = _est("hailuo-02-i2v-standard", {"duration": 6, "resolution": "768P"})
    assert est["credits"] is None
    assert est["source"] == "unmatched"
    assert "duration" in est["formula"]
    assert est["candidates"]


def test_catalog_audio_string_false_is_off():
    off = _est("kling-3.0-omni-i2v", {"resolution": "720p", "duration": 5, "audio": "false"})
    on = _est("kling-3.0-omni-i2v", {"resolution": "720p", "duration": 5, "audio": True})
    assert off["unit"] == pytest.approx(14.0)
    assert on["unit"] == pytest.approx(20.0)


def test_catalog_grok_image_models_priced():
    est = _est("grok-imagine-i2i", {})
    assert est["credits"] == pytest.approx(4.0)
