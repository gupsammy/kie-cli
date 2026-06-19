"""Tests for ledger.py: append, update, rows (newest-first, state filter, limit)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import kie_cli.ledger as ledger


def _ledger_file(tmp_path: Path) -> Path:
    return tmp_path / "tasks.jsonl"


# ── append ────────────────────────────────────────────────────────────────────

def test_append_creates_file(tmp_path):
    ledger.append({"taskId": "t1", "state": "waiting"})
    lf = _ledger_file(tmp_path)
    assert lf.exists()
    lines = [l for l in lf.read_text().splitlines() if l.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["taskId"] == "t1"


def test_append_multiple_rows(tmp_path):
    for i in range(3):
        ledger.append({"taskId": f"t{i}", "state": "waiting"})
    lf = _ledger_file(tmp_path)
    lines = [l for l in lf.read_text().splitlines() if l.strip()]
    assert len(lines) == 3


# ── update ────────────────────────────────────────────────────────────────────

def test_update_merges_kv(tmp_path):
    ledger.append({"taskId": "t1", "state": "waiting", "result_urls": []})
    ledger.update("t1", state="success", result_urls=["https://cdn.kie.ai/vid.mp4"])

    lf = _ledger_file(tmp_path)
    rows = [json.loads(l) for l in lf.read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["state"] == "success"
    assert rows[0]["result_urls"] == ["https://cdn.kie.ai/vid.mp4"]


def test_update_only_matching_taskid(tmp_path):
    ledger.append({"taskId": "t1", "state": "waiting"})
    ledger.append({"taskId": "t2", "state": "waiting"})
    ledger.update("t1", state="success")

    lf = _ledger_file(tmp_path)
    rows = {json.loads(l)["taskId"]: json.loads(l) for l in lf.read_text().splitlines() if l.strip()}
    assert rows["t1"]["state"] == "success"
    assert rows["t2"]["state"] == "waiting"


def test_update_nonexistent_task_noop(tmp_path):
    """Updating a taskId not in the ledger is a no-op."""
    ledger.append({"taskId": "t1", "state": "waiting"})
    ledger.update("nonexistent", state="success")  # should not raise

    lf = _ledger_file(tmp_path)
    rows = [json.loads(l) for l in lf.read_text().splitlines() if l.strip()]
    assert rows[0]["state"] == "waiting"


# ── rows: newest-first ────────────────────────────────────────────────────────

def test_rows_newest_first(tmp_path):
    ledger.append({"taskId": "t1", "state": "success"})
    ledger.append({"taskId": "t2", "state": "waiting"})
    ledger.append({"taskId": "t3", "state": "fail"})

    result = ledger.rows()
    assert result[0]["taskId"] == "t3"  # newest
    assert result[-1]["taskId"] == "t1"  # oldest


# ── rows: state filter ────────────────────────────────────────────────────────

def test_rows_state_filter(tmp_path):
    ledger.append({"taskId": "t1", "state": "success"})
    ledger.append({"taskId": "t2", "state": "waiting"})
    ledger.append({"taskId": "t3", "state": "success"})

    result = ledger.rows(state="success")
    assert len(result) == 2
    assert all(r["state"] == "success" for r in result)


def test_rows_state_filter_no_match(tmp_path):
    ledger.append({"taskId": "t1", "state": "success"})
    result = ledger.rows(state="fail")
    assert result == []


# ── rows: limit ───────────────────────────────────────────────────────────────

def test_rows_limit(tmp_path):
    for i in range(5):
        ledger.append({"taskId": f"t{i}", "state": "success"})

    result = ledger.rows(limit=2)
    assert len(result) == 2
    assert result[0]["taskId"] == "t4"  # newest first, limited to 2


def test_rows_empty_ledger(tmp_path):
    result = ledger.rows()
    assert result == []
