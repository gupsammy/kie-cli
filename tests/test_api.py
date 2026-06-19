"""Tests for api.py: envelope unwrap, error mapping, wait(), download_file."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from kie_cli.api import Client, KieError, _map_envelope_error, download_file
from tests.conftest import json_response, make_transport


# ── _map_envelope_error ───────────────────────────────────────────────────────

@pytest.mark.parametrize("api_code,expected_exit,expected_error_code", [
    (401, 4, "auth_invalid"),
    (402, 7, "insufficient_credits"),
    (429, 5, "rate_limited"),
    (500, 5, "upstream_error"),
    (455, 5, "upstream_error"),
])
def test_map_envelope_error_exit_codes(api_code, expected_exit, expected_error_code):
    err = _map_envelope_error(api_code, "some msg")
    assert err.exit_code == expected_exit
    assert err.code == expected_error_code


def test_map_envelope_error_422_recordinfo_null():
    """422 with 'recordInfo' in the message → task_not_found, exit 3."""
    err = _map_envelope_error(422, "recordInfo is null")
    assert err.exit_code == 3
    assert err.code == "task_not_found"


def test_map_envelope_error_422_other():
    """422 without recordInfo → invalid_params, exit 2."""
    err = _map_envelope_error(422, "validation failed")
    assert err.exit_code == 2
    assert err.code == "invalid_params"


# ── Client.request() envelope unwrap ─────────────────────────────────────────

def _make_client_with_transport(handler) -> Client:
    """Patch httpx.request to use handler; return a Client."""
    transport = make_transport(handler)
    client = Client(api_key="test-key")
    # Monkey-patch: replace httpx.request globally for this test via the module
    return client, transport


def test_client_request_success():
    """200 envelope → returns data."""
    def handler(req):
        return json_response(data={"taskId": "t_123"})

    with patch("httpx.request", side_effect=lambda *a, **kw: json_response(data={"taskId": "t_123"})):
        client = Client(api_key="test-key")
        data = client.request("POST", "/api/v1/jobs/createTask", json={})
    assert data == {"taskId": "t_123"}


def test_client_request_401_raises_auth_invalid():
    with patch("httpx.request", side_effect=lambda *a, **kw: json_response(data=None, code=401, msg="Unauthorized")):
        client = Client(api_key="test-key")
        with pytest.raises(KieError) as exc_info:
            client.request("GET", "/api/v1/chat/credit")
    assert exc_info.value.exit_code == 4
    assert exc_info.value.code == "auth_invalid"


def test_client_request_402_raises_insufficient_credits():
    with patch("httpx.request", side_effect=lambda *a, **kw: json_response(data=None, code=402, msg="Insufficient credits")):
        client = Client(api_key="test-key")
        with pytest.raises(KieError) as exc_info:
            client.request("POST", "/api/v1/jobs/createTask", json={})
    assert exc_info.value.exit_code == 7
    assert exc_info.value.code == "insufficient_credits"


def test_client_request_422_recordinfo_null_exit3():
    with patch("httpx.request", side_effect=lambda *a, **kw: json_response(data=None, code=422, msg="recordInfo is null")):
        client = Client(api_key="test-key")
        with pytest.raises(KieError) as exc_info:
            client.request("GET", "/api/v1/jobs/recordInfo")
    assert exc_info.value.exit_code == 3


def test_client_request_429_rate_limited():
    with patch("httpx.request", side_effect=lambda *a, **kw: json_response(data=None, code=429, msg="Rate limited")):
        client = Client(api_key="test-key")
        with pytest.raises(KieError) as exc_info:
            client.request("POST", "/api/v1/jobs/createTask", json={})
    assert exc_info.value.exit_code == 5
    assert exc_info.value.code == "rate_limited"


def test_client_request_network_error_exit5():
    """httpx.NetworkError → KieError exit 5."""
    def raise_network(*a, **kw):
        raise httpx.NetworkError("connection refused")

    with patch("httpx.request", side_effect=raise_network):
        client = Client(api_key="test-key")
        with pytest.raises(KieError) as exc_info:
            client.request("GET", "/api/v1/chat/credit")
    assert exc_info.value.exit_code == 5
    assert exc_info.value.code == "network_error"


def test_client_missing_api_key_exit4(monkeypatch):
    """Missing KIE_API_KEY → KieError exit 4 on first request."""
    monkeypatch.delenv("KIE_API_KEY", raising=False)
    client = Client(api_key="")
    with pytest.raises(KieError) as exc_info:
        client.request("GET", "/api/v1/chat/credit")
    assert exc_info.value.exit_code == 4
    assert exc_info.value.code == "auth_missing"


# ── Client.wait() ─────────────────────────────────────────────────────────────

def test_wait_success_path():
    """wait() returns record immediately when state is success."""
    call_count = 0

    def mock_get_task(task_id):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {"state": "generating", "resultUrls": []}
        return {"state": "success", "resultUrls": ["https://cdn.example.com/vid.mp4"]}

    client = Client(api_key="test-key")
    with patch.object(client, "get_task", side_effect=mock_get_task):
        with patch("time.sleep"):  # no real sleeping
            rec = client.wait("task_abc", timeout=30)

    assert rec["state"] == "success"
    assert call_count == 2


def test_wait_timeout_exit5():
    """wait() raises KieError(wait_timeout, exit_code=5) on timeout."""
    def mock_get_task(task_id):
        return {"state": "generating", "resultUrls": []}

    client = Client(api_key="test-key")
    # Force immediate timeout: deadline passes after first tick
    with patch.object(client, "get_task", side_effect=mock_get_task):
        with patch("time.sleep"):
            # Use monotonic to simulate time passing by patching monotonic
            times = iter([0.0, 0.0, 999.0])  # start, check in loop, deadline exceeded
            with patch("time.monotonic", side_effect=times):
                with pytest.raises(KieError) as exc_info:
                    client.wait("task_abc", timeout=1)

    assert exc_info.value.exit_code == 5
    assert exc_info.value.code == "wait_timeout"
    assert "task_abc" in exc_info.value.hint


# ── download_file ─────────────────────────────────────────────────────────────

def test_download_file_does_not_call_refresh_endpoint(tmp_path):
    """download_file streams directly from CDN URL — no POST to download-url endpoint."""
    received_urls = []

    def mock_stream(method, url, **kw):
        received_urls.append(url)
        # Build a mock streaming context
        class MockResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def iter_bytes(self, chunk_size=None):
                yield b"fake video data"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        return MockResponse()

    with patch("httpx.stream", side_effect=mock_stream):
        dest = download_file(
            "https://cdn.kie.ai/output/vid.mp4",
            tmp_path,
            "task_xyz-1",
        )

    # Must stream directly from CDN — not POST to /api/v1/common/download-url first
    assert len(received_urls) == 1
    assert received_urls[0] == "https://cdn.kie.ai/output/vid.mp4"
    assert "/download-url" not in received_urls[0]
    assert dest == tmp_path / "task_xyz-1.mp4"
    assert dest.read_bytes() == b"fake video data"


# ── upload() response-shape (live-verified bug: key is downloadUrl, not fileUrl) ──

def _mock_post_envelope(data):
    """Return a function suitable for patching httpx.post with a kie upload envelope."""
    return lambda *a, **kw: httpx.Response(
        200, json={"success": True, "code": 200, "msg": "ok", "data": data}
    )


def test_upload_returns_download_url():
    """Upload server returns the public URL under data.downloadUrl (live-verified)."""
    client = Client(api_key="test-key")
    data = {"downloadUrl": "https://tempfile.redpandaai.co/x/pixel.png", "fileName": "pixel.png"}
    with patch("httpx.post", side_effect=_mock_post_envelope(data)):
        url = client.upload(b"\x89PNG\r\n", filename="pixel.png")
    assert url == "https://tempfile.redpandaai.co/x/pixel.png"


def test_upload_falls_back_to_fileurl():
    """If the endpoint ever returns fileUrl instead, accept it as a fallback."""
    client = Client(api_key="test-key")
    with patch("httpx.post", side_effect=_mock_post_envelope({"fileUrl": "https://x/y.png"})):
        url = client.upload(b"data", filename="y.png")
    assert url == "https://x/y.png"


def test_upload_missing_url_raises_upstream():
    """A response with no recognizable URL key → KieError upstream (exit 5), not KeyError."""
    client = Client(api_key="test-key")
    with patch("httpx.post", side_effect=_mock_post_envelope({"fileName": "z.png"})):
        with pytest.raises(KieError) as exc:
            client.upload(b"data", filename="z.png")
    assert exc.value.exit_code == 5
