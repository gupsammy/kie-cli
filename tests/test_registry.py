"""Tests for registry.py: resolve, build_input, required params, mutual exclusion, type coercion."""
from __future__ import annotations

import pytest

from kie_cli.api import KieError
from kie_cli.registry import resolve


# ── resolve() ────────────────────────────────────────────────────────────────

def test_resolve_exact_id():
    m = resolve("bytedance/seedance-2")
    assert m.id == "bytedance/seedance-2"


def test_resolve_alias():
    m = resolve("seedance-2")
    assert m.id == "bytedance/seedance-2"


def test_resolve_seedance2_mini_alias():
    m = resolve("seedance-2-mini")
    assert m.id == "bytedance/seedance-2-mini"
    # mini caps at 480p/720p like fast; no 1080p
    res = next(p for p in m.params if p.name == "resolution")
    assert res.enum == ["480p", "720p"]
    # mini adds web_search over fast
    assert any(p.name == "web_search" for p in m.params)


def test_seedance2_mini_mutex_first_frame_and_reference_images_exit7():
    """mini inherits the seedance-2 frame/reference mutex."""
    model = resolve("seedance-2-mini")
    with pytest.raises(KieError) as exc_info:
        model.build_input(
            {"image": ["https://ex.com/frame.png"]},
            {"reference_image_urls": ["https://ex.com/ref.png"]},
        )
    assert exc_info.value.exit_code == 7
    assert exc_info.value.code == "conflicting_inputs"


def test_resolve_kling26_alias_routes_to_t2v():
    """SPEC integrator note: kling-2.6 alias → t2v variant."""
    m = resolve("kling-2.6")
    assert m.id == "kling-2.6/text-to-video"


def test_resolve_kling26_i2v_explicit():
    """kling-2.6-i2v must be specified explicitly for i2v."""
    m = resolve("kling-2.6-i2v")
    assert m.id == "kling-2.6/image-to-video"


def test_resolve_unknown_model_exit2():
    with pytest.raises(KieError) as exc_info:
        resolve("nonexistent-model-xyz")
    assert exc_info.value.exit_code == 2
    assert exc_info.value.code == "unknown_model"


# ── build_input: required param enforcement ──────────────────────────────────

def test_v1_pro_i2v_missing_image_exit2():
    """v1-pro-i2v requires image_url; omitting it → invalid_params, exit 2."""
    model = resolve("v1-pro-i2v")
    with pytest.raises(KieError) as exc_info:
        model.build_input({"prompt": "a cat"}, {})
    assert exc_info.value.exit_code == 2
    assert exc_info.value.code == "invalid_params"


def test_v1_pro_i2v_with_image_ok():
    """v1-pro-i2v with image_url → no error."""
    model = resolve("v1-pro-i2v")
    result = model.build_input(
        {"prompt": "a cat", "image": ["https://example.com/img.png"]},
        {}
    )
    assert result["image_url"] == "https://example.com/img.png"
    assert result["prompt"] == "a cat"


def test_seedance15_requires_prompt_and_duration():
    """seedance-1.5-pro requires prompt, duration, aspect_ratio."""
    model = resolve("seedance-1.5-pro")
    with pytest.raises(KieError) as exc_info:
        model.build_input({}, {})
    assert exc_info.value.exit_code == 2
    assert exc_info.value.code == "invalid_params"


# ── build_input: seedance-2 mutual exclusion ─────────────────────────────────

def test_seedance2_mutex_first_frame_and_reference_images_exit7():
    """seedance-2: first_frame_url + reference_image_urls → conflicting_inputs, exit 7."""
    model = resolve("seedance-2")
    with pytest.raises(KieError) as exc_info:
        model.build_input(
            {"image": ["https://ex.com/frame.png"]},  # → first_frame_url
            {"reference_image_urls": ["https://ex.com/ref.png"]},
        )
    assert exc_info.value.exit_code == 7
    assert exc_info.value.code == "conflicting_inputs"


def test_seedance2_no_mutex_violation_ok():
    """seedance-2: first_frame_url only → no error."""
    model = resolve("seedance-2")
    result = model.build_input(
        {"image": ["https://ex.com/frame.png"], "prompt": "fly"},
        {}
    )
    assert "first_frame_url" in result
    assert "reference_image_urls" not in result


# ── build_input: type coercion ────────────────────────────────────────────────

def test_seedance15_duration_string_coercion():
    """seedance-1.5-pro: duration param is type=string; int input → coerced to str."""
    model = resolve("seedance-1.5-pro")
    result = model.build_input(
        {"prompt": "test", "duration": 8, "aspect_ratio": "16:9"},
        {}
    )
    assert result["duration"] == "8"
    assert isinstance(result["duration"], str)


def test_seedance2_duration_int_coercion():
    """seedance-2: duration param is type=integer; str "5" input → coerced to int."""
    model = resolve("seedance-2")
    result = model.build_input(
        {"prompt": "test", "duration": "5"},
        {}
    )
    assert result["duration"] == 5
    assert isinstance(result["duration"], int)


# ── build_input: hailuo image_url is string not list ─────────────────────────

def test_hailuo_image_url_is_string():
    """hailuo-2.3: image_url field must be a string (single_url style), not list."""
    model = resolve("hailuo-2.3")
    result = model.build_input(
        {"prompt": "test", "image": ["https://ex.com/img.png"]},
        {}
    )
    # image_field_style=single_url → result["image_url"] = imgs[0] (string)
    assert isinstance(result["image_url"], str)
    assert result["image_url"] == "https://ex.com/img.png"


# ── build_input: --param passthrough wins over flags ─────────────────────────

def test_param_passthrough_wins_over_flags():
    """raw_params merged last: --param resolution=1080p overrides --resolution 720p."""
    model = resolve("seedance-2")
    result = model.build_input(
        {"prompt": "test", "resolution": "720p"},
        {"resolution": "1080p"},
    )
    assert result["resolution"] == "1080p"


def test_param_passthrough_unknown_key_no_validation():
    """Unknown key in raw_params passes through without error."""
    model = resolve("seedance-2")
    result = model.build_input(
        {"prompt": "test"},
        {"some_future_param": "value"},
    )
    assert result["some_future_param"] == "value"


# ── nano-banana-2/pro: --image flag does not map to image_input ───────────────

def test_nano_banana_2_image_field_style_none():
    """nano-banana-2: image_field_style=none so --image is dropped; use --param image_input=..."""
    model = resolve("nano-banana-2")
    # Passing image via common["image"] should NOT set image_input
    result = model.build_input(
        {"prompt": "test", "image": ["https://ex.com/img.png"]},
        {}
    )
    assert "image_input" not in result
    assert "image_url" not in result


def test_nano_banana_2_param_passthrough_image_input():
    """nano-banana-2: image_input passed via --param works."""
    model = resolve("nano-banana-2")
    result = model.build_input(
        {"prompt": "test"},
        {"image_input": ["https://ex.com/img.png"]},
    )
    assert result["image_input"] == ["https://ex.com/img.png"]


# ── validate_required=False skips required check ─────────────────────────────

def test_build_input_skip_required_validation():
    """validate_required=False: missing required field doesn't raise (used by cost)."""
    model = resolve("v1-pro-i2v")
    # No image provided, but validate_required=False
    result = model.build_input({"prompt": "test"}, {}, validate_required=False)
    assert result["prompt"] == "test"
    assert "image_url" not in result


# ── seedream 5 pro: one CLI model, two upstream endpoints ────────────────────

def test_seedream5pro_alias_and_modes():
    model = resolve("seedream-5-pro")
    assert model.id == "seedream/5-pro-text-to-image"
    assert resolve("seedream/5-pro-image-to-image") is model
    assert model.modes == ["t2i", "i2i"]


def test_seedream5pro_effective_id_routes_on_images():
    model = resolve("seedream-5-pro")
    t2i_inp = model.build_input({"prompt": "a cat"}, {})
    assert model.effective_id(t2i_inp) == "seedream/5-pro-text-to-image"
    i2i_inp = model.build_input(
        {"prompt": "a cat", "image": ["https://ex.com/a.png", "https://ex.com/b.png"]}, {})
    assert i2i_inp["image_urls"] == ["https://ex.com/a.png", "https://ex.com/b.png"]
    assert model.effective_id(i2i_inp) == "seedream/5-pro-image-to-image"


def test_seedream5pro_defaults_applied():
    model = resolve("seedream-5-pro")
    inp = model.build_input({"prompt": "a cat"}, {})
    assert inp["aspect_ratio"] == "1:1"
    assert inp["quality"] == "basic"
    assert inp["output_format"] == "png"


# ── seedance-2-5 ──────────────────────────────────────────────────────────────

def test_resolve_seedance25_aliases():
    for alias in ("seedance-2.5", "seedance-2-5", "bytedance/seedance-2-5"):
        assert resolve(alias).id == "bytedance/seedance-2-5"


def test_seedance25_defaults_and_mutex():
    model = resolve("seedance-2.5")
    inp = model.build_input({"prompt": "x", "duration": 6, "resolution": "720p"}, {})
    assert inp["duration"] == 6 and inp["resolution"] == "720p"
    assert inp["aspect_ratio"] == "adaptive" and inp["output_format"] == "mp4"
    with pytest.raises(KieError) as ei:
        model.build_input({"prompt": "x", "image": ["https://a/first.png"]},
                          {"reference_image_urls": ["https://a/ref.png"]})
    assert ei.value.exit_code == 7


# ── catalog models ────────────────────────────────────────────────────────────

def test_catalog_models_loaded():
    from kie_cli.registry import MODELS
    catalog = [m for m in MODELS.values() if m.source == "catalog"]
    assert len(catalog) >= 100
    assert all(m.doc.startswith("https://docs.kie.ai/market/") for m in catalog)


def test_catalog_alias_and_flag_inference():
    m = resolve("minimax-h3-r2v")
    assert m.id == "minimax-h3/reference-to-video"
    assert m.image_field_style == "list:reference_image_urls"
    inp = m.build_input({"prompt": "p", "image": ["https://a/1.png", "https://a/2.png"],
                         "duration": 6, "resolution": "2K"}, {}, validate_required=False)
    assert inp["reference_image_urls"] == ["https://a/1.png", "https://a/2.png"]
    assert inp["duration"] == 6 and inp["resolution"] == "2K"


def test_catalog_quality_maps_resolution_flag():
    m = resolve("pixverse-v6-t2v")
    inp = m.build_input({"prompt": "p", "resolution": "720p", "audio": False}, {},
                        validate_required=False)
    assert inp["quality"] == "720p"
    assert inp["generate_audio_switch"] is False


def test_catalog_never_shadows_builtin():
    """Ids/aliases hand-written in registry.py win over catalog entries."""
    from kie_cli.registry import MODELS
    assert MODELS["bytedance/seedance-2"].source == "builtin"
    assert MODELS["seedream/5-pro-text-to-image"].source == "builtin"
    assert "seedream/5-pro-image-to-image" not in MODELS  # it is an alias of the builtin
