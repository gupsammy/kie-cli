"""Model registry: definitions, alias resolution, input building.

SPEC §12 / §13.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .api import KieError

# ── Param descriptor ─────────────────────────────────────────────────────────


@dataclass
class Param:
    name: str
    type: str          # 'string' | 'integer' | 'number' | 'boolean' | 'array'
    required: bool = False
    default: Any = None
    enum: list | None = None
    desc: str = ""


# ── Flag mapping ──────────────────────────────────────────────────────────────
# Maps common CLI flag → model input field name.
# Special values:
#   "image_url"  — single string
#   "image_urls" — list (append mode)
#   "first_frame_url" / "last_frame_url" / "end_image_url"
#   None          — flag not supported by this model (silently dropped)

@dataclass
class FlagMap:
    prompt: str | None = "prompt"
    image: str | None = None        # --image
    last_frame: str | None = None   # --last-frame
    duration: str | None = "duration"
    resolution: str | None = "resolution"
    aspect_ratio: str | None = "aspect_ratio"
    audio: str | None = None        # --audio/--no-audio → generate_audio bool
    seed: str | None = None


# ── Model ─────────────────────────────────────────────────────────────────────


@dataclass
class Model:
    id: str
    aliases: list[str]
    kind: str                                   # 'video' | 'image'
    modes: list[str]                            # e.g. ['t2v', 'i2v']
    params: list[Param]
    flag_map: FlagMap
    # image flag style: 'single_url'|'image_urls'|'first_frame_url'|'input_urls'|'image'
    #                   | 'list:<field>' | 'single:<field>' (catalog models, any field name)
    image_field_style: str = "none"
    # mutual exclusion groups: list of sets of input field names
    mutex_groups: list[frozenset] = field(default_factory=list)
    # upstream model id to use instead of `id` when input images are present
    # (for families that split t2i/i2i into separate endpoints)
    i2i_variant_id: str | None = None
    pricing_key: str = ""
    doc: str = ""                # docs.kie.ai page (catalog models)
    source: str = "builtin"      # 'builtin' (hand-written below) | 'catalog' (models-catalog.json)

    def effective_id(self, inp: dict) -> str:
        """Upstream model id for this input."""
        if self.i2i_variant_id and inp.get("image_urls"):
            return self.i2i_variant_id
        return self.id

    def build_input(self, common: dict, raw_params: dict, validate_required: bool = True) -> dict:
        """Map common CLI flags + raw passthrough → final input dict.

        Args:
            common: keys are CLI flag names (prompt, image, last_frame,
                    duration, resolution, aspect_ratio, audio, seed).
            raw_params: raw key=value passthrough (merged last; wins).
            validate_required: if False, skip required-field validation (used by cost command).

        Raises KieError(invalid_params, exit_code=2) for missing required fields.
        Raises KieError(conflicting_inputs, exit_code=7) for mutex violations.
        """
        result: dict = {}
        fm = self.flag_map

        # 1. Apply flag mapping
        if fm.prompt and "prompt" in common:
            result[fm.prompt] = common["prompt"]

        # image / first_frame handling
        if "image" in common and common["image"] is not None:
            imgs: list = common["image"] if isinstance(common["image"], list) else [common["image"]]
            style = self.image_field_style
            if style == "single_url":
                result["image_url"] = imgs[0]
            elif style == "image_urls":
                result["image_urls"] = imgs
            elif style == "first_frame_url":
                result["first_frame_url"] = imgs[0]
                if len(imgs) > 1:
                    result["last_frame_url"] = imgs[1]
            elif style == "input_urls":
                result["input_urls"] = imgs
            elif style == "image":
                result["image"] = imgs[0]
            elif style.startswith("list:"):
                result[style[5:]] = imgs
            elif style.startswith("single:"):
                result[style[7:]] = imgs[0]
            # else: none / unsupported — drop silently (server is truth)

        if "last_frame" in common and common["last_frame"] is not None:
            if fm.last_frame:
                result[fm.last_frame] = common["last_frame"]

        # duration — coerce type
        if fm.duration and "duration" in common and common["duration"] is not None:
            dur = common["duration"]
            dur_param = next((p for p in self.params if p.name == fm.duration), None)
            if dur_param and dur_param.type == "string":
                result[fm.duration] = str(dur)
            else:
                result[fm.duration] = int(dur)

        if fm.resolution and "resolution" in common and common["resolution"] is not None:
            result[fm.resolution] = common["resolution"]

        if fm.aspect_ratio and "aspect_ratio" in common and common["aspect_ratio"] is not None:
            result[fm.aspect_ratio] = common["aspect_ratio"]

        if fm.audio and "audio" in common and common["audio"] is not None:
            result[fm.audio] = bool(common["audio"])

        if fm.seed and "seed" in common and common["seed"] is not None:
            result[fm.seed] = common["seed"]

        # 2. Merge raw_params last (passthrough wins; no validation on unknown keys)
        result.update(raw_params)

        # 3. Apply defaults for required fields that have them; then validate remaining
        for p in self.params:
            if p.name not in result and p.default is not None:
                result[p.name] = p.default

        if validate_required:
            required_params = [p for p in self.params if p.required]
            for p in required_params:
                if p.name not in result:
                    # Build a user-facing hint using CLI flag names
                    cli_hint = _param_to_flag(p.name, self.flag_map)
                    raise KieError(
                        "invalid_params",
                        f"Model {self.id!r} requires {p.name!r} — pass {cli_hint}",
                        hint=f"kie schema {self.aliases[0] if self.aliases else self.id}",
                        exit_code=2,
                    )

        # 4. Enforce mutex groups
        for group in self.mutex_groups:
            present = [k for k in group if k in result]
            if len(present) > 1:
                raise KieError(
                    "conflicting_inputs",
                    f"Mutually exclusive inputs for {self.id!r}: {present}",
                    hint="Use either --image (first/last frame) or reference images, not both",
                    exit_code=7,
                )

        return result


def _param_to_flag(param_name: str, fm: FlagMap) -> str:
    """Map a model param name back to a CLI flag hint string."""
    mapping = {
        fm.prompt: "--prompt/-p",
        fm.duration: "--duration",
        fm.resolution: "--resolution",
        fm.aspect_ratio: "--aspect-ratio",
        fm.audio: "--audio/--no-audio",
        fm.seed: "--seed",
    }
    if fm.last_frame in (param_name, None):
        pass
    hint = mapping.get(param_name)
    if hint:
        return hint
    if param_name in ("image_url", "first_frame_url", "image_urls", "input_urls", "image"):
        return "--image/-i"
    if param_name in ("last_frame_url", "end_image_url"):
        return "--last-frame"
    return f"--param {param_name}=..."


# ── Model definitions ─────────────────────────────────────────────────────────

def _p(name, type_, required=False, default=None, enum=None, desc="") -> Param:
    return Param(name=name, type=type_, required=required, default=default, enum=enum, desc=desc)


MODELS: dict[str, "Model"] = {}

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/seedance-2
# ──────────────────────────────────────────────────────────────────────────────
_sd2 = Model(
    id="bytedance/seedance-2",
    aliases=["seedance-2"],
    kind="video",
    modes=["t2v", "i2v"],
    params=[
        _p("prompt", "string", desc="3-20000 chars"),
        _p("first_frame_url", "string", desc="URL; mutex with reference_image_urls"),
        _p("last_frame_url", "string", desc="Use with first_frame_url"),
        _p("reference_image_urls", "array", desc="Max 9; mutex with first/last frame"),
        _p("reference_video_urls", "array", desc="Max 3 videos; affects billing"),
        _p("reference_audio_urls", "array", desc="Max 3 audio files"),
        _p("generate_audio", "boolean", default=True),
        _p("resolution", "string", default="720p", enum=["480p", "720p", "1080p"]),
        _p("aspect_ratio", "string", default="16:9",
           enum=["1:1", "4:3", "3:4", "16:9", "9:16", "21:9", "adaptive"]),
        _p("duration", "integer", default=5, desc="4-15 seconds"),
        _p("web_search", "boolean"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(
        image=None,   # handled via image_field_style=first_frame_url
        last_frame="last_frame_url",
        audio="generate_audio",
    ),
    image_field_style="first_frame_url",
    # first/last frame params vs reference_image_urls are mutually exclusive
    mutex_groups=[
        frozenset({"first_frame_url", "reference_image_urls"}),
        frozenset({"last_frame_url", "reference_image_urls"}),
    ],
    pricing_key="seedance-2",
)
MODELS[_sd2.id] = _sd2

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/seedance-2-fast
# ──────────────────────────────────────────────────────────────────────────────
_sd2f = Model(
    id="bytedance/seedance-2-fast",
    aliases=["seedance-2-fast"],
    kind="video",
    modes=["t2v", "i2v"],
    params=[
        _p("prompt", "string"),
        _p("first_frame_url", "string"),
        _p("last_frame_url", "string"),
        _p("reference_image_urls", "array", desc="Max 9"),
        _p("reference_video_urls", "array", desc="Max 3"),
        _p("reference_audio_urls", "array"),
        _p("generate_audio", "boolean", default=True),
        _p("resolution", "string", default="720p", enum=["480p", "720p"]),
        _p("aspect_ratio", "string", default="16:9",
           enum=["1:1", "4:3", "3:4", "16:9", "9:16", "21:9", "adaptive"]),
        _p("duration", "integer", default=5, desc="4-15 seconds"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(
        last_frame="last_frame_url",
        audio="generate_audio",
    ),
    image_field_style="first_frame_url",
    mutex_groups=[
        frozenset({"first_frame_url", "reference_image_urls"}),
        frozenset({"last_frame_url", "reference_image_urls"}),
    ],
    pricing_key="seedance-2-fast",
)
MODELS[_sd2f.id] = _sd2f

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/seedance-2-mini
# Same surface as seedance-2-fast (480p/720p ceiling) plus web_search.
# ──────────────────────────────────────────────────────────────────────────────
_sd2m = Model(
    id="bytedance/seedance-2-mini",
    aliases=["seedance-2-mini"],
    kind="video",
    modes=["t2v", "i2v"],
    params=[
        _p("prompt", "string", desc="3-20000 chars"),
        _p("first_frame_url", "string", desc="URL; mutex with reference_image_urls"),
        _p("last_frame_url", "string", desc="Use with first_frame_url"),
        _p("reference_image_urls", "array", desc="Max 9; mutex with first/last frame"),
        _p("reference_video_urls", "array", desc="Max 3 videos; affects billing"),
        _p("reference_audio_urls", "array", desc="Max 3 audio files"),
        _p("generate_audio", "boolean", default=True),
        _p("resolution", "string", default="720p", enum=["480p", "720p"]),
        _p("aspect_ratio", "string", default="16:9",
           enum=["1:1", "4:3", "3:4", "16:9", "9:16", "21:9", "adaptive"]),
        _p("duration", "integer", default=5, desc="4-15 seconds"),
        _p("web_search", "boolean", desc="Online search; text-to-video only"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(
        last_frame="last_frame_url",
        audio="generate_audio",
    ),
    image_field_style="first_frame_url",
    mutex_groups=[
        frozenset({"first_frame_url", "reference_image_urls"}),
        frozenset({"last_frame_url", "reference_image_urls"}),
    ],
    pricing_key="seedance-2-mini",
)
MODELS[_sd2m.id] = _sd2m

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/seedance-2-5
# Same three mutually-exclusive scenarios as seedance-2 (first frame · first+last
# frame · multimodal reference set). Differences: duration 4-30 (-1 = model picks),
# up to 30 reference images / 10 videos / 10 audio, aspect default 'adaptive',
# output_format, return_last_frame. Docs: /market/bytedance/seedance-2-5.
# ──────────────────────────────────────────────────────────────────────────────
_sd25 = Model(
    id="bytedance/seedance-2-5",
    aliases=["seedance-2.5", "seedance-2-5"],
    kind="video",
    modes=["t2v", "i2v", "r2v"],
    params=[
        _p("prompt", "string", desc="max 30000 chars"),
        _p("first_frame_url", "string", desc="URL; mutex with reference_*_urls"),
        _p("last_frame_url", "string", desc="Use with first_frame_url"),
        _p("reference_image_urls", "array", desc="Max 30; mutex with first/last frame"),
        _p("reference_video_urls", "array", desc="Max 10, 2-30s each, <=30s total; affects billing"),
        _p("reference_audio_urls", "array", desc="Max 10, 2-30s each"),
        _p("return_last_frame", "boolean", default=False),
        _p("generate_audio", "boolean", default=True),
        _p("resolution", "string", default="720p", enum=["480p", "720p", "1080p"]),
        _p("aspect_ratio", "string", default="adaptive",
           enum=["1:1", "4:3", "3:4", "16:9", "9:16", "21:9", "adaptive"]),
        _p("duration", "integer", default=5, desc="4-30 seconds; -1 = model picks"),
        _p("output_format", "string", default="mp4", enum=["mp4", "mov"]),
        _p("web_search", "boolean"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(
        last_frame="last_frame_url",
        audio="generate_audio",
    ),
    image_field_style="first_frame_url",
    mutex_groups=[
        frozenset({"first_frame_url", "reference_image_urls"}),
        frozenset({"last_frame_url", "reference_image_urls"}),
    ],
    pricing_key="seedance-2-5",
)
MODELS[_sd25.id] = _sd25

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/seedance-1.5-pro
# ──────────────────────────────────────────────────────────────────────────────
_sd15 = Model(
    id="bytedance/seedance-1.5-pro",
    aliases=["seedance-1.5-pro"],
    kind="video",
    modes=["t2v", "i2v"],
    params=[
        _p("prompt", "string", required=True, desc="3-2500 chars"),
        _p("input_urls", "array", desc="0-2 images for i2v"),
        _p("aspect_ratio", "string", required=True, default="1:1",
           enum=["1:1", "4:3", "3:4", "16:9", "9:16", "21:9"]),
        _p("resolution", "string", default="720p", enum=["480p", "720p", "1080p"]),
        _p("duration", "string", required=True, enum=["4", "8", "12"]),
        _p("fixed_lens", "boolean", default=False),
        _p("generate_audio", "boolean", default=False),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(audio="generate_audio"),
    image_field_style="input_urls",
    pricing_key="seedance-1.5-pro",
)
MODELS[_sd15.id] = _sd15

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/v1-pro-text-to-video
# ──────────────────────────────────────────────────────────────────────────────
_v1pt2v = Model(
    id="bytedance/v1-pro-text-to-video",
    aliases=["v1-pro-t2v"],
    kind="video",
    modes=["t2v"],
    params=[
        _p("prompt", "string", required=True, desc="max 10000 chars"),
        _p("aspect_ratio", "string", default="16:9",
           enum=["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]),
        _p("resolution", "string", default="720p", enum=["480p", "720p", "1080p"]),
        _p("duration", "string", default="5", enum=["5", "10"]),
        _p("camera_fixed", "boolean"),
        _p("seed", "number", default=-1),
        _p("enable_safety_checker", "boolean"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(seed="seed"),
    image_field_style="none",
    pricing_key="v1-pro-t2v",
)
MODELS[_v1pt2v.id] = _v1pt2v

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/v1-pro-image-to-video
# ──────────────────────────────────────────────────────────────────────────────
_v1pi2v = Model(
    id="bytedance/v1-pro-image-to-video",
    aliases=["v1-pro-i2v"],
    kind="video",
    modes=["i2v"],
    params=[
        _p("prompt", "string", required=True),
        _p("image_url", "string", required=True),
        _p("resolution", "string", default="720p", enum=["480p", "720p", "1080p"]),
        _p("duration", "string", default="5", enum=["5", "10"]),
        _p("camera_fixed", "boolean"),
        _p("seed", "number", default=-1),
        _p("enable_safety_checker", "boolean"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(seed="seed"),
    image_field_style="single_url",
    pricing_key="v1-pro-i2v",
)
MODELS[_v1pi2v.id] = _v1pi2v

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/v1-pro-fast-image-to-video
# ──────────────────────────────────────────────────────────────────────────────
_v1pfi2v = Model(
    id="bytedance/v1-pro-fast-image-to-video",
    aliases=["v1-pro-fast-i2v"],
    kind="video",
    modes=["i2v"],
    params=[
        _p("prompt", "string", required=True),
        _p("image_url", "string", required=True),
        _p("resolution", "string", default="720p", enum=["720p", "1080p"]),
        _p("duration", "string", default="5", enum=["5", "10"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(),
    image_field_style="single_url",
    pricing_key="v1-pro-fast-i2v",
)
MODELS[_v1pfi2v.id] = _v1pfi2v

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/v1-lite-text-to-video
# ──────────────────────────────────────────────────────────────────────────────
_v1lt2v = Model(
    id="bytedance/v1-lite-text-to-video",
    aliases=["v1-lite-t2v"],
    kind="video",
    modes=["t2v"],
    params=[
        _p("prompt", "string", required=True),
        _p("aspect_ratio", "string", default="16:9",
           enum=["16:9", "4:3", "1:1", "3:4", "9:16", "9:21"]),
        _p("resolution", "string", default="720p", enum=["480p", "720p", "1080p"]),
        _p("duration", "string", default="5", enum=["5", "10"]),
        _p("camera_fixed", "boolean"),
        _p("seed", "integer"),
        _p("enable_safety_checker", "boolean"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(seed="seed"),
    image_field_style="none",
    pricing_key="v1-lite-t2v",
)
MODELS[_v1lt2v.id] = _v1lt2v

# ──────────────────────────────────────────────────────────────────────────────
# bytedance/v1-lite-image-to-video
# ──────────────────────────────────────────────────────────────────────────────
_v1li2v = Model(
    id="bytedance/v1-lite-image-to-video",
    aliases=["v1-lite-i2v"],
    kind="video",
    modes=["i2v"],
    params=[
        _p("prompt", "string", required=True),
        _p("image_url", "string", required=True),
        _p("end_image_url", "string", desc="Optional end frame"),
        _p("resolution", "string", default="720p", enum=["480p", "720p", "1080p"]),
        _p("duration", "string", default="5", enum=["5", "10"]),
        _p("camera_fixed", "boolean"),
        _p("seed", "number", default=-1),
        _p("enable_safety_checker", "boolean"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(last_frame="end_image_url", seed="seed"),
    image_field_style="single_url",
    pricing_key="v1-lite-i2v",
)
MODELS[_v1li2v.id] = _v1li2v

# ──────────────────────────────────────────────────────────────────────────────
# kling-3.0/video
# ──────────────────────────────────────────────────────────────────────────────
_kling3 = Model(
    id="kling-3.0/video",
    aliases=["kling-3", "kling-3.0"],
    kind="video",
    modes=["t2v", "i2v"],
    params=[
        _p("prompt", "string", required=True),
        # image_urls[0] = first frame, image_urls[1] = last frame (single-shot)
        _p("image_urls", "array", desc="Index 0=first, 1=last frame (single-shot)"),
        _p("sound", "boolean", required=True, default=False),
        _p("duration", "string", required=True, default="5",
           enum=["3","4","5","6","7","8","9","10","11","12","13","14","15"]),
        _p("aspect_ratio", "string", default="16:9", enum=["16:9", "9:16", "1:1"]),
        _p("mode", "string", required=True, default="pro", enum=["std", "pro", "4K"]),
        _p("multi_shots", "boolean", required=True, default=False),
        _p("multi_prompt", "array"),
        _p("kling_elements", "array"),
    ],
    flag_map=FlagMap(
        audio="sound",
        resolution=None,   # resolution is expressed via mode field
    ),
    image_field_style="image_urls",
    pricing_key="kling-3.0",
)
MODELS[_kling3.id] = _kling3

# ──────────────────────────────────────────────────────────────────────────────
# kling-2.6/text-to-video
# ──────────────────────────────────────────────────────────────────────────────
_kling26t = Model(
    id="kling-2.6/text-to-video",
    aliases=["kling-2.6-t2v", "kling-2.6"],
    kind="video",
    modes=["t2v"],
    params=[
        _p("prompt", "string", required=True, desc="max 1000 chars"),
        _p("sound", "boolean", required=True, default=False),
        _p("aspect_ratio", "string", required=True, default="1:1",
           enum=["1:1", "16:9", "9:16"]),
        _p("duration", "string", required=True, default="5", enum=["5", "10"]),
    ],
    flag_map=FlagMap(audio="sound", resolution=None),
    image_field_style="none",
    pricing_key="kling-2.6",
)
MODELS[_kling26t.id] = _kling26t

# ──────────────────────────────────────────────────────────────────────────────
# kling-2.6/image-to-video
# ──────────────────────────────────────────────────────────────────────────────
_kling26i = Model(
    id="kling-2.6/image-to-video",
    aliases=["kling-2.6-i2v"],
    kind="video",
    modes=["i2v"],
    params=[
        _p("prompt", "string", required=True),
        _p("image_urls", "array", required=True, desc="Exactly 1 image URL"),
        _p("sound", "boolean", required=True, default=False),
        _p("duration", "string", required=True, default="5", enum=["5", "10"]),
    ],
    flag_map=FlagMap(audio="sound", resolution=None, aspect_ratio=None),
    image_field_style="image_urls",
    pricing_key="kling-2.6",
)
MODELS[_kling26i.id] = _kling26i

# ──────────────────────────────────────────────────────────────────────────────
# wan/2-6-text-to-video
# ──────────────────────────────────────────────────────────────────────────────
_wan26t = Model(
    id="wan/2-6-text-to-video",
    aliases=["wan-2.6-t2v", "wan-2.6"],
    kind="video",
    modes=["t2v"],
    params=[
        _p("prompt", "string", required=True, desc="1-5000 chars"),
        _p("duration", "string", default="5", enum=["5", "10", "15"]),
        _p("resolution", "string", default="1080p", enum=["720p", "1080p"]),
        _p("nsfw_checker", "boolean"),
    ],
    flag_map=FlagMap(aspect_ratio=None),
    image_field_style="none",
    pricing_key="wan-2.6",
)
MODELS[_wan26t.id] = _wan26t

# ──────────────────────────────────────────────────────────────────────────────
# wan/2-6-image-to-video
# ──────────────────────────────────────────────────────────────────────────────
_wan26i = Model(
    id="wan/2-6-image-to-video",
    aliases=["wan-2.6-i2v"],
    kind="video",
    modes=["i2v"],
    params=[
        _p("prompt", "string", required=True, desc="2-5000 chars"),
        _p("image_urls", "array", required=True, desc="1 image URL"),
        _p("duration", "string", default="5", enum=["5", "10", "15"]),
        _p("resolution", "string", default="1080p", enum=["720p", "1080p"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(aspect_ratio=None),
    image_field_style="image_urls",
    pricing_key="wan-2.6",
)
MODELS[_wan26i.id] = _wan26i

# ──────────────────────────────────────────────────────────────────────────────
# hailuo/2-3-image-to-video-pro
# ──────────────────────────────────────────────────────────────────────────────
_hailuo = Model(
    id="hailuo/2-3-image-to-video-pro",
    aliases=["hailuo-2.3"],
    kind="video",
    modes=["i2v"],
    params=[
        _p("prompt", "string", required=True, desc="max 5000 chars"),
        # single string, NOT array — see research notes
        _p("image_url", "string", required=True),
        _p("duration", "string", default="6", enum=["6", "10"]),
        _p("resolution", "string", default="768P", enum=["768P", "1080P"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(aspect_ratio=None, audio=None),
    image_field_style="single_url",
    pricing_key="hailuo-2.3",
)
MODELS[_hailuo.id] = _hailuo

# ──────────────────────────────────────────────────────────────────────────────
# grok-imagine/text-to-video
# ──────────────────────────────────────────────────────────────────────────────
_grok_t2v = Model(
    id="grok-imagine/text-to-video",
    aliases=["grok-t2v"],
    kind="video",
    modes=["t2v"],
    params=[
        _p("prompt", "string", required=True, desc="max 5000 chars, English"),
        _p("aspect_ratio", "string", default="2:3",
           enum=["2:3", "3:2", "1:1", "16:9", "9:16"]),
        _p("mode", "string", default="normal", enum=["fun", "normal", "spicy"]),
        _p("duration", "number", desc="6-30 integer"),
        _p("resolution", "string", default="480p", enum=["480p", "720p"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(),
    image_field_style="none",
    pricing_key="grok-imagine-video",
)
MODELS[_grok_t2v.id] = _grok_t2v

# ──────────────────────────────────────────────────────────────────────────────
# grok-imagine/image-to-video
# ──────────────────────────────────────────────────────────────────────────────
_grok_i2v = Model(
    id="grok-imagine/image-to-video",
    aliases=["grok-i2v"],
    kind="video",
    modes=["i2v"],
    params=[
        _p("image_urls", "array", desc="max 7 images; mutex with task_id"),
        _p("task_id", "string", desc="prior grok text-to-image task; mutex with image_urls"),
        _p("index", "integer", default=0),
        _p("prompt", "string", desc="max 5000 chars, English"),
        _p("mode", "string", default="normal", enum=["fun", "normal", "spicy"]),
        _p("duration", "string", desc="6-30 integer"),
        _p("resolution", "string", default="480p", enum=["480p", "720p"]),
        _p("aspect_ratio", "string", default="16:9",
           enum=["2:3", "3:2", "1:1", "16:9", "9:16"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(),
    image_field_style="image_urls",
    pricing_key="grok-imagine-video",
)
MODELS[_grok_i2v.id] = _grok_i2v

# ──────────────────────────────────────────────────────────────────────────────
# topaz/video-upscale
# ──────────────────────────────────────────────────────────────────────────────
_topaz_v = Model(
    id="topaz/video-upscale",
    aliases=["topaz-video-upscale"],
    kind="video",
    modes=["upscale"],
    params=[
        _p("video_url", "string", required=True, desc="MP4/MOV/MKV max 50MB"),
        _p("upscale_factor", "string", default="2", enum=["1", "2", "4"]),
    ],
    flag_map=FlagMap(
        prompt=None, duration=None, resolution=None,
        aspect_ratio=None, audio=None, seed=None,
    ),
    image_field_style="none",
    pricing_key="topaz-video-upscale",
)
MODELS[_topaz_v.id] = _topaz_v

# ════════════════════════════════════════════════════════════════════════════
# IMAGE MODELS
# ════════════════════════════════════════════════════════════════════════════

# ──────────────────────────────────────────────────────────────────────────────
# google/nano-banana
# ──────────────────────────────────────────────────────────────────────────────
_nb = Model(
    id="google/nano-banana",
    aliases=["nano-banana"],
    kind="image",
    modes=["t2i"],
    params=[
        _p("prompt", "string", required=True, desc="max 5000"),
        _p("aspect_ratio", "string", default="1:1",
           enum=["1:1", "9:16", "16:9", "3:4", "4:3", "3:2", "2:3", "5:4", "4:5", "21:9", "auto"]),
        _p("output_format", "string", default="png", enum=["png", "jpeg"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(duration=None, resolution=None, audio=None),
    image_field_style="none",
    pricing_key="nano-banana",
)
MODELS[_nb.id] = _nb

# ──────────────────────────────────────────────────────────────────────────────
# google/nano-banana-edit
# ──────────────────────────────────────────────────────────────────────────────
_nbe = Model(
    id="google/nano-banana-edit",
    aliases=["nano-banana-edit"],
    kind="image",
    modes=["i2i"],
    params=[
        _p("prompt", "string", required=True),
        _p("image_urls", "array", required=True, desc="max 10 images"),
        _p("aspect_ratio", "string", default="1:1",
           enum=["1:1", "9:16", "16:9", "3:4", "4:3", "3:2", "2:3", "5:4", "4:5", "21:9", "auto"]),
        _p("output_format", "string", default="png", enum=["png", "jpeg"]),
    ],
    flag_map=FlagMap(duration=None, resolution=None, audio=None),
    image_field_style="image_urls",
    pricing_key="nano-banana",
)
MODELS[_nbe.id] = _nbe

# ──────────────────────────────────────────────────────────────────────────────
# nano-banana-2  (no google/ prefix per research)
# ──────────────────────────────────────────────────────────────────────────────
_nb2 = Model(
    id="nano-banana-2",
    aliases=["nano-banana-2"],
    kind="image",
    modes=["t2i", "i2i"],
    params=[
        _p("prompt", "string", required=True, desc="max 20000"),
        _p("image_input", "array", desc="max 14 reference images"),
        _p("aspect_ratio", "string", default="auto",
           enum=["1:1","1:4","1:8","2:3","3:2","3:4","4:1","4:3","4:5","5:4","8:1","9:16","16:9","21:9","auto"]),
        _p("resolution", "string", default="1K", enum=["1K", "2K", "4K"]),
        _p("output_format", "string", default="jpg", enum=["png", "jpg"]),
    ],
    flag_map=FlagMap(duration=None, audio=None),
    image_field_style="none",   # uses image_input, not standard flag
    pricing_key="nano-banana-2",
)
MODELS[_nb2.id] = _nb2

# ──────────────────────────────────────────────────────────────────────────────
# nano-banana-pro
# ──────────────────────────────────────────────────────────────────────────────
_nbp = Model(
    id="nano-banana-pro",
    aliases=["nano-banana-pro"],
    kind="image",
    modes=["t2i", "i2i"],
    params=[
        _p("prompt", "string", required=True, desc="max 10000"),
        _p("image_input", "array", desc="max 8 reference images"),
        _p("aspect_ratio", "string", default="1:1",
           enum=["1:1","2:3","3:2","3:4","4:3","4:5","5:4","9:16","16:9","21:9","auto"]),
        _p("resolution", "string", default="1K", enum=["1K", "2K", "4K"]),
        _p("output_format", "string", default="png", enum=["png", "jpg"]),
    ],
    flag_map=FlagMap(duration=None, audio=None),
    image_field_style="none",
    pricing_key="nano-banana-pro",
)
MODELS[_nbp.id] = _nbp

# ──────────────────────────────────────────────────────────────────────────────
# seedream/4.5-text-to-image
# ──────────────────────────────────────────────────────────────────────────────
_sd45t = Model(
    id="seedream/4.5-text-to-image",
    aliases=["seedream-4.5-t2i", "seedream-4.5"],
    kind="image",
    modes=["t2i"],
    params=[
        _p("prompt", "string", required=True, desc="max 3000"),
        _p("aspect_ratio", "string", required=True, default="1:1",
           enum=["1:1","4:3","3:4","16:9","9:16","2:3","3:2","21:9"]),
        _p("quality", "string", required=True, default="basic", enum=["basic", "high"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(duration=None, resolution=None, audio=None),
    image_field_style="none",
    pricing_key="seedream-4.5",
)
MODELS[_sd45t.id] = _sd45t

# ──────────────────────────────────────────────────────────────────────────────
# seedream/4.5-edit
# ──────────────────────────────────────────────────────────────────────────────
_sd45e = Model(
    id="seedream/4.5-edit",
    aliases=["seedream-4.5-edit"],
    kind="image",
    modes=["i2i"],
    params=[
        _p("prompt", "string", required=True),
        _p("image_urls", "array", required=True, desc="max 14 images"),
        _p("aspect_ratio", "string", required=True, default="1:1",
           enum=["1:1","4:3","3:4","16:9","9:16","2:3","3:2","21:9"]),
        _p("quality", "string", required=True, default="basic", enum=["basic", "high"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(duration=None, resolution=None, audio=None),
    image_field_style="image_urls",
    pricing_key="seedream-4.5",
)
MODELS[_sd45e.id] = _sd45e

# ──────────────────────────────────────────────────────────────────────────────
# seedream/5-lite-text-to-image
# ──────────────────────────────────────────────────────────────────────────────
_sd5l = Model(
    id="seedream/5-lite-text-to-image",
    aliases=["seedream-5-lite"],
    kind="image",
    modes=["t2i"],
    params=[
        _p("prompt", "string", required=True, desc="3-3000 chars"),
        _p("aspect_ratio", "string", required=True, default="1:1",
           enum=["1:1","4:3","3:4","16:9","9:16","2:3","3:2","21:9"]),
        _p("quality", "string", required=True, default="basic", enum=["basic", "high"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(duration=None, resolution=None, audio=None),
    image_field_style="none",
    pricing_key="seedream-5-lite",
)
MODELS[_sd5l.id] = _sd5l

# ──────────────────────────────────────────────────────────────────────────────
# seedream/5-pro  (one CLI model, two upstream endpoints)
# Upstream splits t2i and i2i into separate model ids with identical params;
# effective_id() routes to the i2i endpoint whenever input images are given.
# ──────────────────────────────────────────────────────────────────────────────
_sd5p = Model(
    id="seedream/5-pro-text-to-image",
    aliases=["seedream-5-pro", "seedream/5-pro-image-to-image"],
    kind="image",
    modes=["t2i", "i2i"],
    params=[
        _p("prompt", "string", required=True, desc="3-5000 chars"),
        _p("image_urls", "array",
           desc="Max 10; jpeg/png/webp, 10MB each; first input image free, 0.5 cr each after"),
        _p("aspect_ratio", "string", required=True, default="1:1",
           enum=["1:1", "4:3", "3:4", "16:9", "9:16", "2:3", "3:2", "21:9"]),
        _p("quality", "string", required=True, default="basic", enum=["basic", "high"],
           desc="basic=1K, high=2K"),
        _p("output_format", "string", default="png", enum=["png", "jpeg"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(duration=None, resolution=None, audio=None),
    image_field_style="image_urls",
    i2i_variant_id="seedream/5-pro-image-to-image",
    pricing_key="seedream-5-pro",
)
MODELS[_sd5p.id] = _sd5p

# ──────────────────────────────────────────────────────────────────────────────
# z-image
# ──────────────────────────────────────────────────────────────────────────────
_zimg = Model(
    id="z-image",
    aliases=["z-image"],
    kind="image",
    modes=["t2i"],
    params=[
        _p("prompt", "string", required=True, desc="max 1000"),
        _p("aspect_ratio", "string", required=True, default="1:1",
           enum=["1:1","4:3","3:4","16:9","9:16"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(duration=None, resolution=None, audio=None),
    image_field_style="none",
    pricing_key="z-image",
)
MODELS[_zimg.id] = _zimg

# ──────────────────────────────────────────────────────────────────────────────
# flux-2/pro-text-to-image
# ──────────────────────────────────────────────────────────────────────────────
_flux2 = Model(
    id="flux-2/pro-text-to-image",
    aliases=["flux-2-pro", "flux-2"],
    kind="image",
    modes=["t2i"],
    params=[
        _p("prompt", "string", required=True, desc="3-5000 chars"),
        _p("aspect_ratio", "string", required=True, default="1:1",
           enum=["1:1","4:3","3:4","16:9","9:16","3:2","2:3"]),
        _p("resolution", "string", required=True, default="1K", enum=["1K", "2K"]),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(duration=None, audio=None),
    image_field_style="none",
    pricing_key="flux-2-pro",
)
MODELS[_flux2.id] = _flux2

# ──────────────────────────────────────────────────────────────────────────────
# qwen/image-edit
# ──────────────────────────────────────────────────────────────────────────────
_qwen = Model(
    id="qwen/image-edit",
    aliases=["qwen-image-edit"],
    kind="image",
    modes=["i2i"],
    params=[
        _p("prompt", "string", required=True, desc="max 2000"),
        _p("image_url", "string", required=True),
        _p("acceleration", "string", default="none", enum=["none", "regular", "high"]),
        _p("image_size", "string", default="landscape_4_3",
           enum=["square","square_hd","portrait_4_3","portrait_16_9","landscape_4_3","landscape_16_9"]),
        _p("num_inference_steps", "number", default=25),
        _p("guidance_scale", "number", default=4),
        _p("seed", "integer"),
        _p("sync_mode", "boolean", default=False),
        _p("num_images", "string", enum=["1","2","3","4"]),
        _p("enable_safety_checker", "boolean", default=True),
        _p("output_format", "string", default="png", enum=["jpeg", "png"]),
        _p("negative_prompt", "string"),
        _p("nsfw_checker", "boolean", default=False),
    ],
    flag_map=FlagMap(
        duration=None, resolution=None, aspect_ratio=None, audio=None, seed="seed",
    ),
    image_field_style="single_url",
    pricing_key="qwen-image-edit",
)
MODELS[_qwen.id] = _qwen

# ──────────────────────────────────────────────────────────────────────────────
# topaz/image-upscale
# ──────────────────────────────────────────────────────────────────────────────
_topaz_img = Model(
    id="topaz/image-upscale",
    aliases=["topaz-image-upscale"],
    kind="image",
    modes=["upscale"],
    params=[
        _p("image_url", "string", required=True),
        _p("upscale_factor", "string", required=True, default="2", enum=["1","2","4","8"]),
    ],
    flag_map=FlagMap(
        prompt=None, duration=None, resolution=None,
        aspect_ratio=None, audio=None, seed=None,
    ),
    image_field_style="single_url",
    pricing_key="topaz-image-upscale",
)
MODELS[_topaz_img.id] = _topaz_img

# ──────────────────────────────────────────────────────────────────────────────
# recraft/remove-background
# ──────────────────────────────────────────────────────────────────────────────
_recraft = Model(
    id="recraft/remove-background",
    aliases=["recraft-remove-bg"],
    kind="image",
    modes=["i2i"],
    params=[
        # Field name is 'image', not 'image_url' — see research notes
        _p("image", "string", required=True, desc="max 5MB, max 4096px"),
    ],
    flag_map=FlagMap(
        prompt=None, duration=None, resolution=None,
        aspect_ratio=None, audio=None, seed=None,
    ),
    image_field_style="image",
    pricing_key="recraft-remove-bg",
)
MODELS[_recraft.id] = _recraft


# ── Catalog models ───────────────────────────────────────────────────────────
# Every other market model comes from data/models-catalog.json, generated from the
# docs.kie.ai OpenAPI pages by research/build_catalog.py. Flag mappings are inferred
# from parameter names; hand-written entries above win on any id/alias collision.
# Pricing for these goes through pricing._est_catalog (generic table lookup).

_CATALOG = Path(__file__).parent / "data" / "models-catalog.json"

_MODE_ABBR = {
    "text-to-video": "t2v", "image-to-video": "i2v", "reference-to-video": "r2v",
    "video-to-video": "v2v", "text-to-image": "t2i", "image-to-image": "i2i",
}

# (param name → image_field_style); first present name wins
_IMAGE_STYLES = [
    ("first_frame_url", "first_frame_url"),
    ("image_urls", "image_urls"),
    ("image_url", "single_url"),
    ("input_urls", "input_urls"),
    ("image", "image"),
    ("reference_image_urls", "list:reference_image_urls"),
    ("image_references", "list:image_references"),
    ("image_input", "list:image_input"),
    ("reference_image", "single:reference_image"),
    ("first_frame_image_url", "single:first_frame_image_url"),
    ("first_frame", "single:first_frame"),
]
_LAST_FRAME_FIELDS = ["last_frame_url", "end_image_url", "tail_image_url", "last_frame_image_url"]
_AUDIO_FIELDS = ["generate_audio", "sound", "audio", "generate_audio_switch"]
_RESOLUTION_FIELDS = ["resolution", "quality"]
_ASPECT_FIELDS = ["aspect_ratio", "ratio"]


def _catalog_alias(mid: str) -> str:
    """'kling-3.0-omni/text-to-video' → 'kling-3.0-omni-t2v'."""
    alias = mid.replace("/", "-")
    for long, short in _MODE_ABBR.items():
        alias = alias.replace(long, short)
    return alias


def _catalog_model(entry: dict) -> Model:
    names = {p["name"] for p in entry["params"]}

    def pick(cands: list[str]) -> str | None:
        return next((c for c in cands if c in names), None)

    mid = entry["id"]
    modes = [short for long, short in _MODE_ABBR.items() if long in mid] or [mid.rsplit("/", 1)[-1]]
    return Model(
        id=mid,
        aliases=[],
        kind=entry["kind"],
        modes=modes,
        params=[Param(name=p["name"], type=p["type"], required=p["required"],
                      default=p.get("default"), enum=p.get("enum"), desc=p.get("desc", ""))
                for p in entry["params"]],
        flag_map=FlagMap(
            prompt="prompt" if "prompt" in names else None,
            last_frame=pick(_LAST_FRAME_FIELDS),
            duration="duration" if "duration" in names else None,
            resolution=pick(_RESOLUTION_FIELDS),
            aspect_ratio=pick(_ASPECT_FIELDS),
            audio=pick(_AUDIO_FIELDS),
            seed="seed" if "seed" in names else None,
        ),
        image_field_style=next((style for name, style in _IMAGE_STYLES if name in names), "none"),
        doc=entry.get("doc", ""),
        source="catalog",
    )


def _load_catalog() -> None:
    try:
        entries = json.loads(_CATALOG.read_text())
    except (OSError, ValueError):
        return  # catalog missing/corrupt → hand-written models only
    taken = set(MODELS) | {a for m in MODELS.values() for a in m.aliases}
    for entry in entries:
        mid = entry.get("id")
        if not mid or mid in taken:
            continue
        model = _catalog_model(entry)
        alias = _catalog_alias(mid)
        if alias != mid and alias not in taken:
            model.aliases = [alias]
            taken.add(alias)
        MODELS[mid] = model
        taken.add(mid)


_load_catalog()


# ── Alias index ──────────────────────────────────────────────────────────────

_ALIAS_INDEX: dict[str, Model] = {}
for _m in MODELS.values():
    for _a in _m.aliases:
        assert _a not in _ALIAS_INDEX, f"Duplicate alias {_a!r}"
        _ALIAS_INDEX[_a] = _m


# ── Public resolver ──────────────────────────────────────────────────────────

def resolve(name: str) -> Model:
    """Return Model for exact id or alias. Raises KieError(unknown_model, 2) if not found."""
    if name in MODELS:
        return MODELS[name]
    if name in _ALIAS_INDEX:
        return _ALIAS_INDEX[name]
    raise KieError(
        "unknown_model",
        f"Unknown model {name!r}",
        hint="kie models --json",
        exit_code=2,
    )
