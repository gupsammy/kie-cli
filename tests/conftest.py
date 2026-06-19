"""Shared fixtures for kie-cli tests.

All tests must be zero-network, zero-credits.
KIE_API_KEY=test-key and KIE_HOME=<tmp> are set for every test.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest


# ── Environment setup ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def kie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set KIE_API_KEY and KIE_HOME for every test; return tmp_path as KIE_HOME."""
    monkeypatch.setenv("KIE_API_KEY", "test-key")
    monkeypatch.setenv("KIE_HOME", str(tmp_path))
    return tmp_path


# ── MockTransport helpers ─────────────────────────────────────────────────────

def make_transport(handler):
    """Wrap a callable(request) -> httpx.Response as an httpx.MockTransport."""
    class _MT(httpx.BaseTransport):
        def handle_request(self, request):
            return handler(request)
    return _MT()


def json_response(data=None, code=200, msg="success", http_status=200) -> httpx.Response:
    """Build a mock httpx.Response with kie envelope."""
    body = {"code": code, "msg": msg, "data": data}
    return httpx.Response(http_status, json=body)
