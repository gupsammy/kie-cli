"""Credit cost estimation from pricing table.

SPEC §11 / §13.

Per-second models: seedance-2, seedance-2-fast, seedance-2-mini, kling-3.0, grok video.
Fixed-SKU models:  kling-2.6, wan-2.6, hailuo-2.3, seedance-1.5-pro, v1-*.
Per-image models:  all image models.

Golden values (from SPEC §15 + research):
  seedance-2  1080p  no-video-input  8s  → 102 cr/s × 8 = 816 credits
  seedance-2-fast 720p no-input      any → 33 cr/s
  z-image                                → 0.8 cr/image
  nano-banana                            → 4 cr/image
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import Model

# ── Constants ─────────────────────────────────────────────────────────────────

CREDIT_USD = 0.005
_CACHE_MAX_AGE = 7 * 24 * 3600   # 7 days in seconds
_BUNDLED = Path(__file__).parent / "data" / "pricing-snapshot.json"


# ── Table loader ─────────────────────────────────────────────────────────────

def load_table(refresh: bool = False) -> list[dict]:
    """Return pricing records.

    Order of preference:
      1. refresh=True → fetch live via Client, write $KIE_HOME/pricing.json.
      2. $KIE_HOME/pricing.json if it exists (warn if > 7 days old).
      3. Bundled data/pricing-snapshot.json.
    """
    kie_home = Path(os.environ.get("KIE_HOME", "~/.kie")).expanduser()
    cache_path = kie_home / "pricing.json"

    if refresh:
        from .api import Client
        records = Client().fetch_pricing()
        kie_home.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"fetched_at": time.time(), "records": records}))
        return records

    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text())
            age = time.time() - payload.get("fetched_at", 0)
            if age > _CACHE_MAX_AGE:
                print(
                    f"Warning: pricing cache is {int(age/86400)} days old; "
                    "run 'kie pricing --refresh' to update.",
                    file=sys.stderr,
                )
            return payload["records"]
        except Exception:
            pass  # fall through to bundled

    # Bundled fallback
    return json.loads(_BUNDLED.read_text())


# ── Internal matchers ─────────────────────────────────────────────────────────

def _find(records: list[dict], *substrings: str, exclude: str | None = None) -> dict | None:
    """Return first record whose modelDescription contains ALL substrings (case-insensitive).
    If exclude is given, skip records whose description contains that string.
    """
    needles = [s.lower() for s in substrings]
    excl = exclude.lower() if exclude else None
    for r in records:
        desc = r.get("modelDescription", "").lower()
        if excl and excl in desc:
            continue
        if all(n in desc for n in needles):
            return r
    return None


def _credits(rec: dict | None) -> float | None:
    if rec is None:
        return None
    v = rec.get("creditPrice")
    if v is None:
        return None
    return float(v)


# ── Per-second estimators ─────────────────────────────────────────────────────

def _est_seedance2(model_id: str, inp: dict, records: list[dict],
                   extra_input_seconds: float = 0.0) -> dict:
    """seedance-2, seedance-2-fast and seedance-2-mini per-second billing."""
    is_fast = "fast" in model_id
    is_mini = "mini" in model_id
    res = inp.get("resolution", "720p").lower()
    has_video_input = bool(inp.get("reference_video_urls"))
    duration = int(inp.get("duration", 5))

    # Build matcher substrings. mini records use a distinct description format:
    # "bytedance/seedance-2-mini, 720P no video" — hyphenated, capital P (folded by
    # the case-insensitive matcher), and "no video"/"with video" WITHOUT the trailing
    # "input" that base/fast carry. That missing suffix also keeps the base lookup
    # below from accidentally matching a mini record.
    if is_mini:
        video_tag = "with video" if has_video_input else "no video"
        rec = _find(records, "bytedance/seedance-2-mini", res, video_tag)
        prefix = "bytedance/seedance-2-mini"
    elif is_fast:
        video_tag = "with video input" if has_video_input else "no video input"
        rec = _find(records, "bytedance/seedance-2 fast", res, video_tag)
        prefix = "bytedance/seedance-2 fast"
    else:
        # Exclude "fast" entries so "bytedance/seedance-2 fast" doesn't match
        video_tag = "with video input" if has_video_input else "no video input"
        rec = _find(records, "bytedance/seedance-2", res, video_tag, exclude="fast")
        prefix = "bytedance/seedance-2"
    unit = _credits(rec)
    if unit is None:
        return {
            "credits": None,
            "usd": None,
            "unit": None,
            "formula": f"{prefix} {res} {video_tag}",
            "source": "unmatched",
            "note": f"No pricing record matched for {prefix!r} {res} {video_tag!r}",
        }

    if has_video_input:
        # credits = unit × (input_duration + output_duration)
        if extra_input_seconds:
            # Known input seconds (e.g. the auto-attached 2s dummy ref).
            credits = unit * (duration + extra_input_seconds)
            note = None
            formula = f"{unit} cr/s × ({extra_input_seconds:g}s ref + {duration}s out)"
        else:
            # User-supplied ref of unknown duration — output only, with caveat.
            credits = unit * duration
            note = "input duration unknown; using output duration only. Actual cost = unit × (input_s + output_s)"
            formula = f"{unit} cr/s × {duration}s (output only)"
    else:
        credits = unit * duration
        note = None
        formula = f"{unit} cr/s × {duration}s"

    result: dict = {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": unit,
        "formula": formula,
        "source": "estimate",
    }
    if note:
        result["note"] = note
    return result


def _est_kling3(inp: dict, records: list[dict]) -> dict:
    """kling-3.0 per-second billing by mode (resolution) + audio."""
    mode = inp.get("mode", "pro").lower()
    sound = inp.get("sound", False)
    duration = int(inp.get("duration", 5))

    # mode → resolution label in pricing
    res_label = {"std": "720P", "pro": "1080P", "4k": "4K"}.get(mode, "1080P")
    audio_tag = "with audio" if sound else "without audio"

    rec = _find(records, "Kling 3.0", audio_tag + "-" + res_label)
    if rec is None:
        # Try alternate format
        rec = _find(records, "Kling 3.0", res_label, audio_tag)
    unit = _credits(rec)
    if unit is None:
        return {
            "credits": None, "usd": None, "unit": None,
            "formula": f"Kling 3.0 {res_label} {audio_tag}",
            "source": "unmatched",
            "note": f"No pricing record matched for Kling 3.0 {res_label} {audio_tag}",
        }

    credits = unit * duration
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": unit,
        "formula": f"{unit} cr/s × {duration}s ({res_label}, {audio_tag})",
        "source": "estimate",
    }


def _est_grok_video(model_id: str, inp: dict, records: list[dict]) -> dict:
    """grok-imagine video per-second billing."""
    res = inp.get("resolution", "480p").lower()
    duration = int(inp.get("duration", 10))

    # The pricing records show two grok video entries:
    # "grok-imagine-video-1-5-preview" (per-second) and "grok-imagine" (older, per-second)
    rec = _find(records, "grok-imagine-video-1-5-preview", res)
    if rec is None:
        rec = _find(records, "grok-imagine", res)
        if rec and "image-to-image" in rec.get("modelDescription", "").lower():
            rec = None  # wrong table entry
    unit = _credits(rec)
    if unit is None:
        return {
            "credits": None, "usd": None, "unit": None,
            "formula": f"grok video {res}",
            "source": "unmatched",
            "note": f"No pricing record matched for grok video {res}",
        }

    credits = unit * duration
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": unit,
        "formula": f"{unit} cr/s × {duration}s ({res})",
        "source": "estimate",
    }


# ── Fixed-SKU estimators ──────────────────────────────────────────────────────

def _est_kling26(model_id: str, inp: dict, records: list[dict]) -> dict:
    """kling-2.6 fixed SKU lookup."""
    mode_label = "text-to-video" if "text" in model_id else "image-to-video"
    sound = inp.get("sound", False)
    dur = inp.get("duration", "5")
    dur_str = str(float(dur))   # matches "5.0s" / "10.0s" format in records

    audio_tag = "with audio" if sound else "without audio"
    rec = _find(records, "kling 2.6", mode_label, audio_tag + "-" + dur_str + "s")
    if rec is None:
        rec = _find(records, "kling 2.6", mode_label, audio_tag, dur_str + "s")
    credits = _credits(rec)
    if credits is None:
        return {
            "credits": None, "usd": None, "unit": None,
            "formula": f"kling 2.6 {mode_label} {dur}s {audio_tag}",
            "source": "unmatched",
            "note": "No pricing record matched",
        }
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits ({mode_label} {dur}s {audio_tag})",
        "source": "estimate",
    }


def _est_wan26(model_id: str, inp: dict, records: list[dict]) -> dict:
    """wan 2.6 fixed SKU lookup."""
    mode_label = "text to video" if "text" in model_id else "image-to-video"
    res = inp.get("resolution", "1080p").lower()
    dur = inp.get("duration", "5")
    dur_str = str(float(dur)) + "s"

    rec = _find(records, "wan 2.6", mode_label, dur_str + "-" + res)
    if rec is None:
        rec = _find(records, "wan 2.6", mode_label, dur_str, res)
    credits = _credits(rec)
    if credits is None:
        return {
            "credits": None, "usd": None, "unit": None,
            "formula": f"wan 2.6 {mode_label} {dur}s {res}",
            "source": "unmatched",
            "note": "No pricing record matched",
        }
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits ({mode_label} {dur}s {res})",
        "source": "estimate",
    }


def _est_hailuo(inp: dict, records: list[dict]) -> dict:
    """hailuo 2.3 fixed SKU lookup. Always Pro tier (2-3-image-to-video-pro)."""
    dur = inp.get("duration", "6")
    dur_str = str(float(dur)) + "s"
    res = inp.get("resolution", "768P")
    # Normalize resolution to match record format
    res_norm = res.upper().replace("P", "p").replace("p", "P")  # keep 768P / 1080P casing

    rec = _find(records, "hailuo 2.3", "Pro", dur_str + "-" + res_norm)
    if rec is None:
        rec = _find(records, "hailuo 2.3", "Pro", dur_str, res_norm)
    credits = _credits(rec)
    if credits is None:
        return {
            "credits": None, "usd": None, "unit": None,
            "formula": f"hailuo 2.3 Pro {dur}s {res_norm}",
            "source": "unmatched",
            "note": "No pricing record matched",
        }
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits (Pro {dur}s {res_norm})",
        "source": "estimate",
    }


def _est_seedance15(inp: dict, records: list[dict]) -> dict:
    """seedance-1.5-pro fixed SKU — not in live records; use research table values.

    Research shows seedance-1.5-pro uses a fixed pricing table similar to V1 series.
    Not separately listed in the live pricing endpoint under its model ID — the per-second
    entries for seedance-2 cover that family. Fall through to unknown.
    """
    # The live records don't have a clear seedance-1.5-pro fixed SKU entry.
    return {
        "credits": None, "usd": None, "unit": None,
        "formula": "seedance-1.5-pro (fixed SKU not in live pricing table)",
        "source": "unmatched",
        "note": "Pricing for seedance-1.5-pro not found in live records; refresh pricing cache.",
    }


def _est_v1(model_id: str, inp: dict, records: list[dict]) -> dict:
    """bytedance V1 series — not in live pricing table either (legacy)."""
    return {
        "credits": None, "usd": None, "unit": None,
        "formula": f"{model_id} (not in pricing table)",
        "source": "unmatched",
        "note": "V1 series pricing not found in live records.",
    }


# ── Per-image estimators ──────────────────────────────────────────────────────

def _est_nano_banana(model_id: str, inp: dict, records: list[dict]) -> dict:
    """nano-banana flat 4 credits/image."""
    rec = _find(records, "Google nano banana", "text-to-image")
    credits = _credits(rec) or 4.0
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image",
        "source": "estimate",
    }


def _est_nano_banana_edit(records: list[dict]) -> dict:
    rec = _find(records, "Google nano banana edit", "image-to-image")
    credits = _credits(rec) or 4.0
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image",
        "source": "estimate",
    }


def _est_nano_banana_2(inp: dict, records: list[dict]) -> dict:
    res = inp.get("resolution", "1K").upper()
    rec = _find(records, "Google nano banana 2", res)
    credits = _credits(rec) or {"1K": 8.0, "2K": 12.0, "4K": 18.0}.get(res, 8.0)
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image ({res})",
        "source": "estimate",
    }


def _est_nano_banana_pro(inp: dict, records: list[dict]) -> dict:
    res = inp.get("resolution", "1K").upper()
    if res in ("1K", "2K"):
        rec = _find(records, "Google nano banana pro", "1/2K")
    else:
        rec = _find(records, "Google nano banana pro", "4K")
    credits = _credits(rec) or {"1K": 18.0, "2K": 18.0, "4K": 24.0}.get(res, 18.0)
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image ({res})",
        "source": "estimate",
    }


def _est_seedream45(model_id: str, records: list[dict]) -> dict:
    mode = "image-to-image" if "edit" in model_id else "text-to-image"
    rec = _find(records, "seedream 4.5", mode)
    credits = _credits(rec) or 6.5
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image",
        "source": "estimate",
    }


def _est_seedream5lite(model_id: str, records: list[dict]) -> dict:
    mode = "image-to-image" if "edit" in model_id else "text-to-image"
    rec = _find(records, "seedream 5.0 Lite", mode)
    credits = _credits(rec) or 5.5
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image",
        "source": "estimate",
    }


def _est_z_image(records: list[dict]) -> dict:
    rec = _find(records, "Qwen z-image")
    credits = _credits(rec) or 0.8
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image",
        "source": "estimate",
    }


def _est_flux2(inp: dict, records: list[dict]) -> dict:
    res = inp.get("resolution", "1K").upper()
    mode = "image to image" if inp.get("image_url") or inp.get("image_urls") else "text-to-image"
    rec = _find(records, "flux-2 pro", mode, res)
    if rec is None:
        rec = _find(records, "flux-2 pro", res)
    credits = _credits(rec) or {"1K": 5.0, "2K": 7.0}.get(res, 5.0)
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image ({res})",
        "source": "estimate",
    }


def _est_qwen_image_edit(records: list[dict]) -> dict:
    # Live records show "Qwen image-edit" at 5 per megapixel.
    # We don't know image dimensions here; return null with note.
    return {
        "credits": None, "usd": None, "unit": None,
        "formula": "qwen/image-edit (5 credits/megapixel; dimensions unknown)",
        "source": "unmatched",
        "note": "Cost depends on output megapixels; use --param to estimate manually.",
    }


def _est_topaz_image(inp: dict, records: list[dict]) -> dict:
    factor = inp.get("upscale_factor", "2")
    # Live records: 2K=10, 4K=20, 8K=40 per image
    target_res = {"1": "2K", "2": "2K", "4": "4K", "8": "8K"}.get(str(factor), "2K")
    rec = _find(records, "Topaz Image Upscaler", target_res)
    credits = _credits(rec) or {"2K": 10.0, "4K": 20.0, "8K": 40.0}.get(target_res, 10.0)
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image (upscale {factor}x → {target_res})",
        "source": "estimate",
    }


def _est_recraft(records: list[dict]) -> dict:
    rec = _find(records, "Recraft Remove Background")
    credits = _credits(rec) or 1.0
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": credits,
        "formula": f"fixed {credits} credits/image",
        "source": "estimate",
    }


def _est_topaz_video(inp: dict, records: list[dict]) -> dict:
    """Topaz video upscale per-second billing."""
    factor = inp.get("upscale_factor", "2")
    if factor in ("1", "2"):
        rec = _find(records, "Topaz Video Upscaler", "1x/2x")
    else:
        rec = _find(records, "Topaz Video Upscaler", "4x")
    unit = _credits(rec)
    if unit is None:
        return {
            "credits": None, "usd": None, "unit": None,
            "formula": f"Topaz video upscale {factor}x",
            "source": "unmatched",
            "note": "No pricing record matched",
        }
    # Duration of source video unknown here; return per-second rate only
    return {
        "credits": None,
        "usd": None,
        "unit": unit,
        "formula": f"{unit} cr/s × video_duration (upscale {factor}x)",
        "source": "estimate",
        "note": "Video duration unknown; credits = unit × source_seconds",
    }


# ── Public API ────────────────────────────────────────────────────────────────

def estimate(model: "Model", inp: dict, extra_input_seconds: float = 0.0) -> dict:
    """Estimate credits for a generation.

    extra_input_seconds: known reference-video input seconds to bill on top of
        output duration (e.g. the auto-attached 2s dummy ref). Only affects the
        per-second seedance-2 SKUs; ignored elsewhere.

    Returns:
        dict with keys: credits (float|None), usd (float|None), unit (float|None),
                        formula (str), source ('estimate'|'unmatched')
        Never raises — on any failure returns credits=None with note.
    """
    try:
        return _estimate_inner(model, inp, extra_input_seconds)
    except Exception as exc:
        return {
            "credits": None,
            "usd": None,
            "unit": None,
            "formula": "error",
            "source": "unmatched",
            "note": str(exc),
        }


def _estimate_inner(model: "Model", inp: dict, extra_input_seconds: float = 0.0) -> dict:
    records = load_table()
    mid = model.id

    # ── Video: per-second ────────────────────────────────────────────────────
    if mid in ("bytedance/seedance-2", "bytedance/seedance-2-fast",
               "bytedance/seedance-2-mini"):
        return _est_seedance2(mid, inp, records, extra_input_seconds)

    if mid == "kling-3.0/video":
        return _est_kling3(inp, records)

    if mid in ("grok-imagine/text-to-video", "grok-imagine/image-to-video"):
        return _est_grok_video(mid, inp, records)

    if mid == "topaz/video-upscale":
        return _est_topaz_video(inp, records)

    # ── Video: fixed SKU ─────────────────────────────────────────────────────
    if mid in ("kling-2.6/text-to-video", "kling-2.6/image-to-video"):
        return _est_kling26(mid, inp, records)

    if mid in ("wan/2-6-text-to-video", "wan/2-6-image-to-video"):
        return _est_wan26(mid, inp, records)

    if mid == "hailuo/2-3-image-to-video-pro":
        return _est_hailuo(inp, records)

    if mid == "bytedance/seedance-1.5-pro":
        return _est_seedance15(inp, records)

    if mid.startswith("bytedance/v1-"):
        return _est_v1(mid, inp, records)

    # ── Image: flat per image ─────────────────────────────────────────────────
    if mid == "google/nano-banana":
        return _est_nano_banana(mid, inp, records)

    if mid == "google/nano-banana-edit":
        return _est_nano_banana_edit(records)

    if mid == "nano-banana-2":
        return _est_nano_banana_2(inp, records)

    if mid == "nano-banana-pro":
        return _est_nano_banana_pro(inp, records)

    if mid in ("seedream/4.5-text-to-image", "seedream/4.5-edit"):
        return _est_seedream45(mid, records)

    if mid == "seedream/5-lite-text-to-image":
        return _est_seedream5lite(mid, records)

    if mid == "z-image":
        return _est_z_image(records)

    if mid == "flux-2/pro-text-to-image":
        return _est_flux2(inp, records)

    if mid == "qwen/image-edit":
        return _est_qwen_image_edit(records)

    if mid == "topaz/image-upscale":
        return _est_topaz_image(inp, records)

    if mid == "recraft/remove-background":
        return _est_recraft(records)

    return {
        "credits": None,
        "usd": None,
        "unit": None,
        "formula": f"unknown model {mid!r}",
        "source": "unmatched",
        "note": f"No pricing estimator for model {mid!r}",
    }
