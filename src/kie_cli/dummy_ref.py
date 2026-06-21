"""Attach a blank 2s video reference to seedance-2 generations that have no video
reference of their own. ON by default; auto-skipped when the caller already supplies
a real video ref; opt OUT manually with --no-dummy-ref.

Default-on for cost: most generations have no video ref and bill cheaper with one
attached (rationale below), so the saving is the common case and the dummy is
attached automatically. When a real video ref is present (e.g. a continuity/extend
clip) the dummy is skipped — nothing to do, and --no-dummy-ref is unnecessary.

Does the dummy hurt verbatim dialogue? No. An earlier scare blamed dropped dialogue
on the dummy's r2v mode, but the regression was traced to PROMPT LENGTH, not the ref:
prompts over ~3000 chars paraphrase; lean prompts render the lines verbatim even with
the dummy attached (controlled test — a ~3979-char prompt paraphrased; a ~2400-char
prompt with the same video ref rendered verbatim). Keep prompts ≤~3000 chars and the
dummy is free. --no-dummy-ref is a manual escape hatch: reach for it only if a
dialogue regression reappears on an already-lean prompt.

Cost rationale (when you do opt in): seedance-2 / seedance-2-fast bill per second
on two SKUs — a pricier "no video input" rate and a cheaper "with video input" rate
charged on (input_s + output_s). A throwaway 2s clip flips onto the cheaper SKU; the
2 extra input-seconds cost less than the rate discount for output >= the 4s minimum.

The blank clip (black, silent, 2s, 1280x720) ships as package data at
data/blank-2s.mp4 and is served from the public repo, so kie fetches it by URL — no
upload, no per-machine state. Dimensions are load-bearing: the r2v input floor is
409600 px and 1.8s, so the clip must be >=640x640-equivalent and >=2s.
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
