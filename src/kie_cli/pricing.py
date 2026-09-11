"""Credit cost estimation from pricing table.

SPEC §11 / §13.

Per-second models: seedance-2, seedance-2-fast, seedance-2-mini, seedance-2-5, kling-3.0, grok video.
Fixed-SKU models:  kling-2.6, wan-2.6, hailuo-2.3, seedance-1.5-pro, v1-*.
Per-image models:  all image models.
Catalog models:    generic lookup (_est_catalog) — hint table narrows the live pricing
                   rows by the tokens the input implies; one row → estimate, several →
                   candidates listed for the caller to pick.

Golden values (from SPEC §15 + research):
  seedance-2  1080p  no-video-input  8s  → 102 cr/s × 8 = 816 credits
  seedance-2-fast 720p no-input      any → 24.8 cr/s (snapshot 2026-09-11)
  z-image                                → 0.8 cr/image
  nano-banana                            → 4 cr/image
"""
from __future__ import annotations

import json
import os
import re
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
    """seedance-2, -fast, -mini and -2-5 per-second billing."""
    is_fast = "fast" in model_id
    is_mini = "mini" in model_id
    is_25 = "seedance-2-5" in model_id
    res = inp.get("resolution", "720p").lower()
    has_video_input = bool(inp.get("reference_video_urls"))
    try:
        duration = int(inp.get("duration", 5))
    except (TypeError, ValueError):
        duration = 0

    # Build matcher substrings. mini records use a distinct description format:
    # "bytedance/seedance-2-mini, 720P no video" — hyphenated, capital P (folded by
    # the case-insensitive matcher), and "no video"/"with video" WITHOUT the trailing
    # "input" that base/fast carry. That missing suffix also keeps the base lookup
    # below from accidentally matching a mini record.
    if is_25 or is_mini:
        # short-tag rows: "bytedance/seedance-2-5, 720p with video" / "...-2-mini, 720P no video"
        prefix = "bytedance/seedance-2-5" if is_25 else "bytedance/seedance-2-mini"
        video_tag = "with video" if has_video_input else "no video"
        rec = _find(records, prefix, res, video_tag)
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

    if duration <= 0:
        # -1 = "model picks" (seedance-2-5); the render may run to the 30s cap.
        return {
            "credits": None, "usd": None, "unit": unit,
            "formula": f"{unit} cr/s × output_seconds ({prefix} {res} {video_tag})",
            "source": "estimate",
            "note": "Output duration unknown (duration <= 0 lets the model choose); "
                    "credits = unit × (input_s + output_s).",
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

    # Rows: "grok-imagine, text-to-video, 1080p" / "grok-imagine, image-to-video, 720p".
    # Match on the mode label so the upscale / image rows can never be picked.
    mode_label = "text-to-video" if "text" in model_id else "image-to-video"
    rec = _find(records, "grok-imagine,", mode_label, res)
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


def _est_seedream5pro(inp: dict, records: list[dict]) -> dict:
    """seedream 5 Pro per-image billing from the live table: quality basic→1K /
    high→2K record, plus the 'input image' record per input after the first
    (first input image free). All rates come from pricing records — no literals."""
    n_inputs = len(inp.get("image_urls") or [])
    mode = "image-to-image" if n_inputs else "text-to-image"
    res = "2K" if inp.get("quality", "basic") == "high" else "1K"
    rec = _find(records, "seedream 5 Pro", mode, res)
    base = _credits(rec)
    if base is None:
        return {
            "credits": None, "usd": None, "unit": None,
            "formula": f"seedream 5 Pro {mode} {res}",
            "source": "unmatched",
            "note": "No pricing record matched; run 'kie pricing --refresh'.",
        }

    billable_inputs = max(0, n_inputs - 1)
    surcharge = 0.0
    if billable_inputs:
        in_rec = _find(records, "seedream 5 Pro", "input image")
        unit_in = _credits(in_rec)
        if unit_in is None:
            return {
                "credits": None, "usd": None, "unit": base,
                "formula": f"seedream 5 Pro {mode} {res} + input image surcharge",
                "source": "unmatched",
                "note": "No 'input image' pricing record matched; run 'kie pricing --refresh'.",
            }
        surcharge = unit_in * billable_inputs

    credits = base + surcharge
    if billable_inputs:
        formula = (f"{base} cr/image ({res}) + {surcharge:g} cr "
                   f"({billable_inputs} extra input image{'s' if billable_inputs > 1 else ''}; first free)")
    else:
        formula = f"fixed {base} credits/image ({res})"
    return {
        "credits": credits,
        "usd": round(credits * CREDIT_USD, 4),
        "unit": base,
        "formula": formula,
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


# ── Catalog models: generic table lookup ─────────────────────────────────────
# (model-id prefix, substrings every candidate row must contain, substring to exclude).
# First matching prefix wins, so list the more specific id before its parent.
# Rows are then narrowed by what the input implies (resolution/quality/mode tokens,
# duration, audio on/off, video-input present). See _est_catalog.
_CATALOG_HINTS: list[tuple[str, list[str], str | None]] = [
    # video
    ("google/gemini-omni-flash-1-1", ["google/gemini-omni-flash-1-1"], None),
    ("gemini-omni-video", ["gemini-omni-video"], None),
    ("wan/3-0-video-prime", ["wan3.0 video prime"], None),
    ("wan/3-0-video", ["wan 3.0 video"], None),
    ("pixverse-v6/extend", ["pixverse-v6", "extend"], None),
    ("pixverse-v6/reference-to-video", ["pixverse-v6", "reference to video"], None),
    ("pixverse-v6/", ["pixverse-v6", "text /image to video"], None),      # t2v · i2v · transition
    ("minimax-h3/text-to-video", ["minimax h3", "text to video"], None),
    ("minimax-h3/image-to-video", ["minimax h3", "image to video"], None),
    ("minimax-h3/reference-to-video", ["minimax h3", "reference to video"], None),
    ("happyhorse-1-1/text-to-video", ["happyhorse-1.1", "text-to-video"], None),
    ("happyhorse-1-1/image-to-video", ["happyhorse-1.1", "image-to-video"], None),
    ("happyhorse-1-1/reference-to-video", ["happyhorse-1.1", "reference-to-video"], None),
    ("happyhorse/text-to-video", ["happyhorse-1.0", "text-to-video"], None),
    ("happyhorse/image-to-video", ["happyhorse-1.0", "image-to-video"], None),
    ("happyhorse/reference-to-video", ["happyhorse-1.0", "reference-to-video"], None),
    ("happyhorse/video-edit", ["happyhorse-1.0", "video-edit"], None),
    ("kling/v3-turbo-text-to-video", ["kling 3.0 turbo", "text-to-video"], None),
    ("kling/v3-turbo-image-to-video", ["kling 3.0 turbo", "image-to-video"], None),
    ("kling-3.0-omni/", ["kling 3.0, video"], None),                      # shares the Kling 3.0 table
    ("kling-3.0/motion-control", ["kling 3.0 motion control"], None),
    ("kling-2.6/motion-control", ["kling 2.6 motion control"], None),
    ("kling/v2-5-turbo-text-to-video-pro", ["kling 2.5 turbo", "text-to-video"], None),
    ("kling/v2-5-turbo-image-to-video-pro", ["kling 2.5 turbo", "image-to-video"], None),
    ("kling/v2-1-master-text-to-video", ["kling 2.1", "text-to-video", "master"], None),
    ("kling/v2-1-master-image-to-video", ["kling 2.1", "image-to-video", "master"], None),
    ("kling/v2-1-pro", ["kling 2.1", "pro-"], None),
    ("kling/v2-1-standard", ["kling 2.1", "standard-"], None),
    ("kling/ai-avatar-standard", ["kling ai avtar", "standard"], None),
    ("kling/ai-avatar-pro", ["kling ai avtar", "pro"], None),
    ("volcengine/video-to-video-lip-sync", ["volcengine"], None),
    ("omnihuman-1-5", ["omnihuman-1-5"], None),
    ("infinitalk/from-audio", ["infinitetalk"], None),
    ("wan/2-7-text-to-video", ["wan 2.7 video", "text-to-video"], None),
    ("wan/2-7-image-to-video", ["wan 2.7 video", "image-to-video"], None),
    ("wan/2-7-r2v", ["wan 2.7 video", "r2v"], None),
    ("wan/2-7-videoedit", ["wan 2.7 video", "videoedit"], None),
    ("wan/2-6-video-to-video", ["wan 2.6", "video-to-video"], None),
    ("wan/2-6-flash-image-to-video", ["wan 2.6", "image-to-video"], None),   # no flash rows; base rate
    ("wan/2-6-flash-video-to-video", ["wan 2.6", "video-to-video"], None),
    ("wan/2-5-text-to-video", ["wan 2.5", "text-to-video"], None),
    ("wan/2-5-image-to-video", ["wan 2.5", "image-to-video"], None),
    ("wan/2-2-a14b-text-to-video-turbo", ["wan 2.2,", "text-to-video"], None),
    ("wan/2-2-a14b-image-to-video-turbo", ["wan 2.2,", "image-to-video"], None),
    ("wan/2-2-a14b-speech-to-video-turbo", ["speech to video"], None),
    ("wan/2-2-animate-move", ["animate move"], None),
    ("wan/2-2-animate-replace", ["animate replace"], None),
    ("hailuo/2-3-image-to-video-standard", ["hailuo 2.3", "standard-"], None),
    ("hailuo/02-text-to-video-standard", ["hailuo 02", "text-to-video", "standard-"], None),
    ("hailuo/02-text-to-video-pro", ["hailuo 02", "text-to-video", "pro-"], None),
    ("hailuo/02-image-to-video-standard", ["hailuo 02", "image-to-video", "standard-"], None),
    ("hailuo/02-image-to-video-pro", ["hailuo 02", "image-to-video", "pro-"], None),
    ("grok-imagine/extend", ["grok-imagine/extend"], None),
    ("grok-imagine/upscale", ["grok-imagine", "upscale"], None),
    ("grok-imagine-video-1-5-preview", ["grok-imagine-video-1-5-preview"], None),
    # image
    ("gpt-image-2-5-sunburst-text-to-image", ["gpt-image-2-5-sunburst", "text-to-image"], None),
    ("gpt-image-2-5-sunburst-image-to-image", ["gpt-image-2-5-sunburst", "image-to-image"], None),
    ("gpt-image-2-5-flare-text-to-image", ["gpt-image-2-5-flare", "text-to-image"], None),
    ("gpt-image-2-5-flare-image-to-image", ["gpt-image-2-5-flare", "image-to-image"], None),
    ("gpt-image-2-text-to-image", ["gpt image 2,", "text-to-image"], None),
    ("gpt-image-2-image-to-image", ["gpt image 2,", "image-to-image"], None),
    ("gpt-image/1.5-text-to-image", ["gpt image 1.5", "text-to-image"], None),
    ("gpt-image/1.5-image-to-image", ["gpt image 1.5", "image-to-image"], None),
    ("grok-imagine/text-to-image", ["grok-imagine, text-to-image"], None),
    ("grok-imagine/image-to-image", ["grok-imagine, image-to-image"], None),
    ("grok-imagine-image-2-0/text-to-image", ["grok-imagine-image-2-0", "text to image"], None),
    ("grok-imagine-image-2-0/", ["grok-imagine-image-2-0", "image edit"], None),
    ("qwen3/pro-text-to-image", ["qwen image 3.0 pro", "text to image"], None),
    ("qwen3/pro-image-to-image", ["qwen image 3.0 pro", "output"], None),
    ("qwen3/text-to-image", ["qwen image 3.0,", "text to image"], None),
    ("qwen3/image-to-image", ["qwen image 3.0,", "output"], None),
    ("nano-banana-2-lite", ["nano-banana-2-lite"], None),
    ("wan/2-7-image-pro", ["wan 2.7 image pro"], None),
    ("wan/2-7-image", ["wan 2.7 image"], "pro"),
    ("qwen2/text-to-image", ["qwen2", "text-to-image"], None),
    ("qwen2/image-edit", ["qwen2", "image-to-image"], None),
    ("flux-2/flex-text-to-image", ["flux 2 flex", "text to image"], None),
    ("flux-2/flex-image-to-image", ["flux 2 flex", "image to image"], None),
    ("flux-2/pro-image-to-image", ["flux-2 pro", "image to image"], None),
    ("seedream/5-lite-image-to-image", ["seedream 5.0 lite", "image-to-image"], None),
    ("seedream/5-pro-layer-decomposition", ["seedream 5 pro", "layer decomposition"], None),
    ("recraft/crisp-upscale", ["recraft crisp upscale"], None),
    ("ideogram/v3-text-to-image", ["ideogram v3,"], None),
    ("ideogram/v3-edit", ["ideogram v3-edit"], None),
    ("ideogram/v3-remix", ["ideogram v3-remix"], None),
    ("ideogram/character-edit", ["ideogram character-edit"], None),
    ("ideogram/character-remix", ["ideogram character-remix"], None),
    ("ideogram/character", ["ideogram character,"], None),
    ("google/imagen4-fast", ["google imagen4", "fast"], None),
    ("google/imagen4-ultra", ["google imagen4", "ultra"], None),
    ("google/imagen4", ["google imagen4", "default"], None),
    ("qwen/text-to-image", ["qwen image ,", "text-to-image"], None),        # per megapixel
    ("qwen/image-to-image", ["qwen image,", "image-to-image"], None),
]

# Input fields whose value (lower-cased) may appear verbatim in a row description.
_TOKEN_FIELDS = ("resolution", "quality", "rendering_speed", "mode", "output_resolution")
_AUDIO_ON = ("with audio", "with aiduo")
_AUDIO_OFF = ("no audio", "without audio", "no aiduo")
_AUDIO_ANY = _AUDIO_ON + _AUDIO_OFF
_DUR_RE = re.compile(r"(?<![0-9.])(\d+(?:\.\d+)?)s(?![a-z0-9])")


def _narrow(cands: list[dict], keep, applicable: bool | None = None) -> list[dict]:
    """Filter candidate rows.

    applicable=True  → the rows encode this token kind, so an empty result is a real
                       miss (no SKU for that value) and [] is returned.
    applicable=False → the rows don't encode it; skip the filter.
    applicable=None  → unknown; keep the filter only if it leaves something.
    """
    if applicable is False:
        return cands
    kept = [r for r in cands if keep(r["modelDescription"].lower())]
    if applicable is None:
        return kept or cands
    return kept


def _as_bool(v) -> bool:
    """Coerce raw --param / --input-json values: 'false', '0', 'off', 'no' → False."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "off", "no", "none", "null")
    return bool(v)


def _unmatched_value(mid: str, key: str, val, rows: list[dict]) -> dict:
    return {
        "credits": None, "usd": None, "unit": None,
        "formula": f"{mid}: no pricing row for {key}={val}",
        "source": "unmatched",
        "candidates": [{"description": r["modelDescription"],
                        "credits": _credits(r), "unit": r.get("creditUnit")} for r in rows],
        "note": f"No pricing row for {key}={val!r}; see 'candidates' for the SKUs that exist.",
    }


def _est_catalog(model: "Model", inp: dict, records: list[dict],
                 extra_input_seconds: float = 0.0) -> dict:
    """Generic estimate for catalog models (see _CATALOG_HINTS)."""
    mid = model.id
    hint = next((h for h in _CATALOG_HINTS if mid.startswith(h[0])), None)
    if hint is None:
        return {"credits": None, "usd": None, "unit": None,
                "formula": f"{mid} (no pricing hint)", "source": "unmatched",
                "note": f"No pricing hint for {mid!r}; check 'kie pricing --model <substr>'."}
    _, needles, exclude = hint
    cands = [r for r in records
             if all(n in r.get("modelDescription", "").lower() for n in needles)
             and not (exclude and exclude in r.get("modelDescription", "").lower())]
    if not cands:
        return {"credits": None, "usd": None, "unit": None,
                "formula": f"{mid} ({' + '.join(needles)})", "source": "unmatched",
                "note": "No pricing record matched; run 'kie pricing --refresh'."}

    descs = [r["modelDescription"].lower() for r in cands]
    enums = {p.name: [str(e).lower() for e in p.enum] for p in model.params if p.enum}

    # video input present / absent — first, because with-video rows may carry no
    # duration token (gemini-omni) and would be lost to the duration filter.
    has_video = bool(inp.get("reference_video_urls") or inp.get("video_urls")
                     or inp.get("video_url") or inp.get("video_list"))
    if any("video input" in d for d in descs):
        tok = "with video" if has_video else "no video"
        before = cands
        cands = _narrow(cands, lambda d: tok in d, applicable=True)
        if not cands:
            return _unmatched_value(mid, "video_input", has_video, before)

    # value tokens: resolution / quality / rendering_speed / mode. Applicable when the
    # rows mention any of the param's enum values; then a miss is a real miss.
    for key in _TOKEN_FIELDS:
        val = inp.get(key)
        if val is None:
            continue
        tok = str(val).lower()
        applicable = True if any(e in d for e in enums.get(key, []) for d in descs) else None
        before = cands
        cands = _narrow(cands, lambda d, t=tok: t in d, applicable)
        if not cands:
            return _unmatched_value(mid, key, val, before)

    # duration-keyed rows ("5.0s", "10s")
    dur = inp.get("duration")
    if dur is not None and any(_DUR_RE.search(r["modelDescription"]) for r in cands):
        try:
            want = float(dur)
        except (TypeError, ValueError):
            want = None
        if want is not None and want > 0:
            before = cands
            cands = _narrow(cands, lambda d: any(float(m) == want for m in _DUR_RE.findall(d)),
                            applicable=True)
            if not cands:
                return _unmatched_value(mid, "duration", dur, before)

    # audio on/off
    audio = next((inp[k] for k in ("generate_audio", "sound", "audio", "generate_audio_switch")
                  if k in inp), None)
    if audio is not None and any(t in d for d in descs for t in _AUDIO_ANY):
        toks = _AUDIO_ON if _as_bool(audio) else _AUDIO_OFF
        cands = _narrow(cands, lambda d: any(t in d for t in toks), applicable=True) or cands

    if len(cands) > 1:
        return {
            "credits": None, "usd": None, "unit": None,
            "formula": f"{mid}: {len(cands)} pricing rows match",
            "source": "ambiguous",
            "candidates": [{"description": r["modelDescription"],
                            "credits": _credits(r), "unit": r.get("creditUnit")} for r in cands],
            "note": "Several pricing rows match; pass --resolution/--duration/--audio (or --param) "
                    "to pick one, or read 'candidates'.",
        }

    rec = cands[0]
    desc = rec.get("modelDescription", "")
    unit = _credits(rec)
    cu = (rec.get("creditUnit") or "").lower()
    if unit is None:
        return {"credits": None, "usd": None, "unit": None, "formula": desc,
                "source": "unmatched", "note": "Pricing row has no credit price."}

    if "per second" in cu:
        duration = inp.get("duration")
        if duration is None:
            dur_param = next((p for p in model.params if p.name == "duration"), None)
            duration = dur_param.default if dur_param else None
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = None
        if duration is None or duration <= 0:
            # 0 = "follow the input video" (wan videoedit), -1 = model picks.
            return {"credits": None, "usd": None, "unit": unit,
                    "formula": f"{unit} cr/s × output_seconds ({desc})", "source": "estimate",
                    "note": "Output duration unknown; credits = unit × seconds."}
        ref = extra_input_seconds if ("with video" in desc.lower() and extra_input_seconds) else 0.0
        credits = unit * (duration + ref)
        formula = (f"{unit} cr/s × ({ref:g}s ref + {duration:g}s out)" if ref
                   else f"{unit} cr/s × {duration:g}s") + f" ({desc})"
        return {"credits": credits, "usd": round(credits * CREDIT_USD, 4), "unit": unit,
                "formula": formula, "source": "estimate"}

    if "megapixel" in cu:
        return {"credits": None, "usd": None, "unit": unit,
                "formula": f"{unit} cr/megapixel ({desc})", "source": "estimate",
                "note": "Cost depends on output megapixels."}

    # per video / image / request / generation / upscale / blank
    n = inp.get("num_images") or inp.get("n") or 1
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 1
    credits = unit * n
    formula = f"fixed {unit} credits{f' × {n}' if n > 1 else ''} ({desc})"
    return {"credits": credits, "usd": round(credits * CREDIT_USD, 4), "unit": unit,
            "formula": formula, "source": "estimate"}


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
               "bytedance/seedance-2-mini", "bytedance/seedance-2-5"):
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

    if mid == "seedream/5-pro-text-to-image":
        return _est_seedream5pro(inp, records)

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

    # ── Catalog models: generic table lookup ─────────────────────────────────
    if getattr(model, "source", "builtin") == "catalog":
        return _est_catalog(model, inp, records, extra_input_seconds)

    return {
        "credits": None,
        "usd": None,
        "unit": None,
        "formula": f"unknown model {mid!r}",
        "source": "unmatched",
        "note": f"No pricing estimator for model {mid!r}",
    }
