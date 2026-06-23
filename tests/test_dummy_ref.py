"""Tests for the auto-attached blank video ref (dummy_ref.py) and its effect on
seedance-2 cost estimates. Zero-network."""
from __future__ import annotations

import pytest

from kie_cli import dummy_ref, pricing
from kie_cli.registry import resolve


# ── Eligibility / decision logic ──────────────────────────────────────────────

@pytest.mark.parametrize("model_id", [
    "bytedance/seedance-2", "bytedance/seedance-2-fast", "bytedance/seedance-2-mini",
])
def test_wants_dummy_eligible_no_ref(model_id):
    assert dummy_ref.wants_dummy(model_id, {}, enabled=True) is True


def test_wants_dummy_skipped_when_user_supplied_ref():
    inp = {"reference_video_urls": ["https://example.com/mine.mp4"]}
    assert dummy_ref.wants_dummy("bytedance/seedance-2", inp, enabled=True) is False


def test_wants_dummy_skipped_when_disabled():
    assert dummy_ref.wants_dummy("bytedance/seedance-2", {}, enabled=False) is False


@pytest.mark.parametrize("model_id", ["bytedance/seedance-1.5-pro", "kling-3.0/video", "z-image"])
def test_wants_dummy_ineligible_models(model_id):
    assert dummy_ref.wants_dummy(model_id, {}, enabled=True) is False


# ── Cost effect ───────────────────────────────────────────────────────────────

def _est_with_dummy(alias, inp):
    """Estimate as cmd_generate does: inject the dummy ref + bill its 2s."""
    inp = dict(inp)
    inp["reference_video_urls"] = [dummy_ref.DUMMY_REF_URL]
    return pricing.estimate(resolve(alias), inp,
                            extra_input_seconds=dummy_ref.DUMMY_REF_SECONDS)


def test_dummy_ref_cheaper_than_no_ref_480p_5s():
    """480p seedance-2, 5s: dummy = 11.5×(2+5)=80.5 < no-ref 19×5=95."""
    with_dummy = _est_with_dummy("seedance-2", {"resolution": "480p", "duration": 5})
    no_ref = pricing.estimate(resolve("seedance-2"), {"resolution": "480p", "duration": 5})
    assert with_dummy["credits"] == pytest.approx(80.5)
    assert no_ref["credits"] == pytest.approx(95.0)
    assert with_dummy["credits"] < no_ref["credits"]
    assert "2s ref" in with_dummy["formula"]


def test_dummy_ref_win_at_minimum_4s_floor():
    """Even at the 4s minimum output, the 2s dummy wins: 11.5×6=69 < 19×4=76."""
    with_dummy = _est_with_dummy("seedance-2", {"resolution": "480p", "duration": 4})
    no_ref = pricing.estimate(resolve("seedance-2"), {"resolution": "480p", "duration": 4})
    assert with_dummy["credits"] == pytest.approx(69.0)
    assert with_dummy["credits"] < no_ref["credits"]


def test_dummy_ref_cheaper_for_mini_480p_4s():
    """mini bills on its own SKU format: dummy = 6×(2+4)=36 < no-ref 9.5×4=38."""
    with_dummy = _est_with_dummy("seedance-2-mini", {"resolution": "480p", "duration": 4})
    no_ref = pricing.estimate(resolve("seedance-2-mini"), {"resolution": "480p", "duration": 4})
    assert with_dummy["credits"] == pytest.approx(36.0)
    assert no_ref["credits"] == pytest.approx(38.0)
    assert with_dummy["credits"] < no_ref["credits"]


def test_user_ref_estimate_keeps_unknown_input_caveat():
    """A user-supplied ref (no known input seconds) bills output-only with a note."""
    est = pricing.estimate(
        resolve("seedance-2"),
        {"resolution": "480p", "duration": 5,
         "reference_video_urls": ["https://example.com/mine.mp4"]},
    )
    assert est["credits"] == pytest.approx(57.5)  # 11.5 × 5, output only
    assert "note" in est
