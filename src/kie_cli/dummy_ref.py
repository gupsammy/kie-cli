"""Auto-attach a blank 2s video reference to seedance-2 generations that have
no video reference of their own.

Why: seedance-2 / seedance-2-fast bill per second on two distinct SKUs — a
pricier "no video input" rate and a cheaper "with video input" rate charged on
(input_s + output_s). Attaching a throwaway 2s clip flips a text-to-video or
image-to-video request onto the cheaper SKU; the 2 extra input-seconds cost far
less than the rate discount for any output >= the 4s minimum. See pricing.py.

The blank clip (black, silent, 2s, ~3KB) ships as package data at
data/blank-2s.mp4 and is also served from the public repo, so kie fetches it
directly by URL — no upload, no cache, no per-machine state.
"""
from __future__ import annotations

# Models with a cheaper "with video input" per-second SKU that accept
# reference_video_urls. Only these are eligible.
_ELIGIBLE = {"bytedance/seedance-2", "bytedance/seedance-2-fast"}

# Duration of the blank clip, in seconds. Drives the cost estimate.
DUMMY_REF_SECONDS = 2.0

# Public raw URL of the bundled blank clip. Pinned to main (self-healing if the
# file moves); the asset is effectively immutable so the branch ref is stable.
DUMMY_REF_URL = (
    "https://raw.githubusercontent.com/gupsammy/kie-cli/main/src/kie_cli/data/blank-2s.mp4"
)


def is_eligible(model_id: str) -> bool:
    return model_id in _ELIGIBLE


def wants_dummy(model_id: str, inp: dict, enabled: bool) -> bool:
    """True when the dummy ref should be attached: feature enabled, an eligible
    model, and the caller supplied no video reference of their own."""
    return enabled and is_eligible(model_id) and not inp.get("reference_video_urls")
