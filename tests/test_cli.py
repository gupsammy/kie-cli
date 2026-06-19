"""Tests for cli.py: JSON shapes, dry-run, confirmation gate, --max-credits, error output."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kie_cli.api import KieError
from kie_cli.cli import (
    _cost_gate,
    _handle_error,
    build_parser,
    cmd_cost,
    cmd_models,
    cmd_schema,
    main,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse(args: list[str]):
    parser = build_parser()
    return parser.parse_args(args)


def _run_main(argv: list[str], monkeypatch, env_patch=None) -> tuple[int, str, str]:
    """Run main() with given argv; return (exit_code, stdout, stderr)."""
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    exit_code = 0
    monkeypatch.setattr(sys, "argv", ["kie"] + argv)
    with patch("sys.stdout", captured_out), patch("sys.stderr", captured_err):
        try:
            main()
        except SystemExit as e:
            exit_code = int(e.code) if e.code is not None else 0
    return exit_code, captured_out.getvalue(), captured_err.getvalue()


# ── cmd_models: NDJSON shape ──────────────────────────────────────────────────

def test_models_json_ndjson_shape(monkeypatch, capsys):
    """kie models --json outputs one JSON object per line (NDJSON)."""
    args = _parse(["models", "--json"])
    from kie_cli.api import Client
    client = Client(api_key="test-key")
    with patch.object(client, "request"):
        cmd_models(args, client)

    captured = capsys.readouterr()
    lines = [l for l in captured.out.splitlines() if l.strip()]
    assert len(lines) > 0
    for line in lines:
        obj = json.loads(line)
        assert "id" in obj
        assert "aliases" in obj
        assert "kind" in obj
        assert "modes" in obj


def test_models_json_kind_filter(capsys):
    """kie models --kind image --json returns only image models."""
    args = _parse(["models", "--kind", "image", "--json"])
    from kie_cli.api import Client
    client = Client(api_key="test-key")
    cmd_models(args, client)

    captured = capsys.readouterr()
    lines = [l for l in captured.out.splitlines() if l.strip()]
    assert len(lines) > 0
    for line in lines:
        obj = json.loads(line)
        assert obj["kind"] == "image"


# ── cmd_schema: JSON shape ────────────────────────────────────────────────────

def test_schema_json_shape(capsys):
    """kie schema seedance-2 --json → {model, params: [{name, type, required, ...}]}."""
    args = _parse(["schema", "seedance-2", "--json"])
    from kie_cli.api import Client
    client = Client(api_key="test-key")
    cmd_schema(args, client)

    captured = capsys.readouterr()
    obj = json.loads(captured.out)
    assert obj["model"] == "bytedance/seedance-2"
    assert isinstance(obj["params"], list)
    assert len(obj["params"]) > 0
    # Each param must have name, type, required
    for p in obj["params"]:
        assert "name" in p
        assert "type" in p
        assert "required" in p


# ── cmd_cost: JSON shape ──────────────────────────────────────────────────────

def test_cost_json_shape(capsys, monkeypatch):
    """kie cost seedance-2 1080p 8s previews the dummy-ref price by default:
    62×(2+8)=620, matching what generate auto-attaches."""
    args = _parse(["cost", "seedance-2", "--duration", "8", "--resolution", "1080p",
                   "--no-audio", "--json"])
    from kie_cli.api import Client
    client = Client(api_key="test-key")

    # Mock credit balance call
    with patch.object(client, "request", return_value=1000.0):
        cmd_cost(args, client)

    captured = capsys.readouterr()
    obj = json.loads(captured.out)
    assert obj["model"] == "bytedance/seedance-2"
    assert obj["credits"] == pytest.approx(620.0)
    assert "2s ref" in obj["formula"]
    assert "sufficient" in obj


def test_cost_no_dummy_ref_shows_full_price(capsys):
    """--no-dummy-ref restores the un-optimized 1080p 8s price: 102×8=816."""
    args = _parse(["cost", "seedance-2", "--duration", "8", "--resolution", "1080p",
                   "--no-audio", "--no-dummy-ref", "--json"])
    from kie_cli.api import Client
    client = Client(api_key="test-key")

    with patch.object(client, "request", return_value=1000.0):
        cmd_cost(args, client)

    obj = json.loads(capsys.readouterr().out)
    assert obj["credits"] == 816
    assert obj["usd"] == pytest.approx(4.08)


def test_cost_json_unknown_sku_credits_none(capsys):
    """Unknown SKU → credits=None in JSON output, no crash."""
    args = _parse(["cost", "seedance-1.5-pro", "--duration", "8", "--json"])
    from kie_cli.api import Client
    client = Client(api_key="test-key")

    with patch.object(client, "request", return_value=1000.0):
        cmd_cost(args, client)

    captured = capsys.readouterr()
    obj = json.loads(captured.out)
    assert obj["credits"] is None


# ── dry-run: never POSTs ──────────────────────────────────────────────────────

def test_dry_run_makes_no_post(monkeypatch, capsys):
    """--dry-run prints request body and exits 0 without calling createTask."""
    create_task_called = []

    def fail_on_create(*a, **kw):
        create_task_called.append(True)
        raise AssertionError("createTask must NOT be called in --dry-run")

    args = _parse([
        "generate", "z-image",
        "-p", "neon koi",
        "--dry-run", "--json",
    ])
    from kie_cli.api import Client
    client = Client(api_key="test-key")

    with patch.object(client, "create_task", side_effect=fail_on_create):
        from kie_cli.cli import cmd_generate
        exit_code = cmd_generate(args, client)

    assert exit_code == 0
    assert not create_task_called
    captured = capsys.readouterr()
    obj = json.loads(captured.out)
    assert "request" in obj
    assert "estimate" in obj
    assert obj["request"]["model"] == "z-image"


# ── confirmation gate ─────────────────────────────────────────────────────────

def test_confirmation_gate_non_tty_without_yes_exit7():
    """Non-TTY, est > threshold, no --yes → KieError confirmation_required, exit 7."""
    est = {"credits": 100.0, "usd": 0.5}  # above default threshold of 50
    args = MagicMock()
    args.yes = False

    # Simulate non-TTY by making stdin/stdout not a tty
    with patch("sys.stdin") as mock_stdin, patch("sys.stdout") as mock_stdout:
        mock_stdin.isatty.return_value = False
        mock_stdout.isatty.return_value = False
        with pytest.raises(KieError) as exc_info:
            _cost_gate(est, yes=False, max_credits=None, use_json=False,
                       model_id="bytedance/seedance-2", args=args)

    assert exc_info.value.exit_code == 7
    assert exc_info.value.code == "confirmation_required"
    assert "--yes" in exc_info.value.hint


def test_confirmation_gate_yes_bypasses_threshold():
    """--yes skips confirmation prompt even when est > threshold."""
    est = {"credits": 200.0, "usd": 1.0}
    args = MagicMock()
    # Should NOT raise
    _cost_gate(est, yes=True, max_credits=None, use_json=False,
               model_id="test", args=args)


def test_confirmation_gate_under_threshold_proceeds():
    """est <= 50 credits (default threshold) → no prompt, no raise."""
    est = {"credits": 10.0, "usd": 0.05}
    args = MagicMock()
    _cost_gate(est, yes=False, max_credits=None, use_json=False,
               model_id="test", args=args)


def test_confirmation_gate_credits_none_skips_gate():
    """credits=None (unknown pricing) → gate is skipped, no raise."""
    est = {"credits": None, "usd": None}
    args = MagicMock()
    # Should NOT raise even with --max-credits set
    _cost_gate(est, yes=False, max_credits=50.0, use_json=False,
               model_id="test", args=args)


# ── --max-credits hard cap (not bypassed by --yes) ────────────────────────────

def test_max_credits_exceeded_exit7():
    """est > --max-credits → max_credits_exceeded, exit 7."""
    est = {"credits": 200.0, "usd": 1.0}
    args = MagicMock()
    with pytest.raises(KieError) as exc_info:
        _cost_gate(est, yes=False, max_credits=100.0, use_json=False,
                   model_id="test-model", args=args)
    assert exc_info.value.exit_code == 7
    assert exc_info.value.code == "max_credits_exceeded"


def test_max_credits_not_bypassed_by_yes():
    """--yes does NOT bypass --max-credits hard cap."""
    est = {"credits": 300.0, "usd": 1.5}
    args = MagicMock()
    with pytest.raises(KieError) as exc_info:
        _cost_gate(est, yes=True, max_credits=100.0, use_json=False,
                   model_id="test-model", args=args)
    assert exc_info.value.exit_code == 7
    assert exc_info.value.code == "max_credits_exceeded"


# ── error JSON goes to stderr, not stdout ────────────────────────────────────

def test_error_json_to_stderr(monkeypatch, capsys):
    """--json errors → stderr, stdout is clean."""
    err = KieError("auth_invalid", "Invalid API key", hint=None, exit_code=4)
    _handle_error(err, use_json=True)

    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout
    err_obj = json.loads(captured.err)
    assert err_obj["error"] == "auth_invalid"
    assert "message" in err_obj


def test_error_json_shape(capsys):
    """JSON error envelope has error, message, hint keys."""
    err = KieError("rate_limited", "Too many requests", hint="retry later", exit_code=5)
    _handle_error(err, use_json=True)

    captured = capsys.readouterr()
    obj = json.loads(captured.err)
    assert "error" in obj
    assert "message" in obj
    assert "hint" in obj
    assert obj["error"] == "rate_limited"
    assert obj["hint"] == "retry later"


# ── exit code mapping via main() ──────────────────────────────────────────────

def test_main_unknown_model_exit2(monkeypatch):
    """kie cost totally-unknown-model → exit 2."""
    monkeypatch.setenv("KIE_API_KEY", "test-key")
    code, out, err = _run_main(["cost", "totally-unknown-model-xyz"], monkeypatch)
    assert code == 2


def test_main_missing_api_key_exit4(monkeypatch):
    """kie balance without KIE_API_KEY → exit 4."""
    monkeypatch.delenv("KIE_API_KEY", raising=False)
    monkeypatch.setenv("KIE_HOME", "/tmp/kie-test-empty")

    def mock_credit(*a, **kw):
        raise KieError("auth_missing", "KIE_API_KEY not set", exit_code=4)

    with patch("kie_cli.cli.Client") as MockClient:
        instance = MockClient.return_value
        instance.credit.side_effect = KieError("auth_missing", "KIE_API_KEY not set", exit_code=4)
        code, out, err = _run_main(["balance"], monkeypatch)
    assert code == 4


# ── generate --json compound result shape ────────────────────────────────────

def test_generate_json_compound_result_shape(monkeypatch, capsys):
    """generate --json result includes all required compound fields."""
    mock_record = {
        "state": "success",
        "resultUrls": [],
        "creditsConsumed": 0.8,
        "resultJson": '{"resultUrls":[]}',
    }

    args = _parse([
        "generate", "z-image",
        "-p", "test prompt",
        "--yes", "--no-download", "--json",
    ])
    from kie_cli.api import Client
    client = Client(api_key="test-key")

    with patch.object(client, "create_task", return_value="task_test_123"), \
         patch.object(client, "wait", return_value={
             "state": "success",
             "resultUrls": [],
             "creditsConsumed": 0.8,
             "resultJson": {"resultUrls": []},
         }), \
         patch("kie_cli.cli.download_file") as mock_dl:
        from kie_cli.cli import cmd_generate
        exit_code = cmd_generate(args, client)

    assert exit_code == 0
    captured = capsys.readouterr()
    obj = json.loads(captured.out)

    required_keys = {"taskId", "model", "state", "est_credits",
                     "credits_consumed", "usd", "result_urls", "files",
                     "created_at", "elapsed_s"}
    assert required_keys.issubset(obj.keys()), f"Missing keys: {required_keys - obj.keys()}"
    assert obj["taskId"] == "task_test_123"
    assert obj["model"] == "z-image"
    assert obj["state"] == "success"


# ── _normalize_ts (live-verified bug: status emitted raw epoch millis) ─────────

def test_normalize_ts_epoch_millis_to_iso():
    """Live status API returns createTime as epoch millis → normalize to ISO-8601."""
    from kie_cli.cli import _normalize_ts
    assert _normalize_ts(1781078058047) == "2026-06-10T07:54:18Z"
    assert _normalize_ts("1781078058047") == "2026-06-10T07:54:18Z"


def test_normalize_ts_passthrough_and_none():
    """Already-ISO strings pass through unchanged; falsy → None."""
    from kie_cli.cli import _normalize_ts
    assert _normalize_ts("2026-06-10T07:54:17Z") == "2026-06-10T07:54:17Z"
    assert _normalize_ts(None) is None
    assert _normalize_ts(0) is None


def test_normalize_ts_epoch_seconds():
    """Bare epoch seconds (< 1e12) are treated as seconds, not millis."""
    from kie_cli.cli import _normalize_ts
    assert _normalize_ts(1781078058) == "2026-06-10T07:54:18Z"
