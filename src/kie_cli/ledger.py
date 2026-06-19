"""Local task ledger — append-only JSONL with in-place update support."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _ledger_path() -> Path:
    kie_home = Path(os.environ.get("KIE_HOME", "~/.kie")).expanduser()
    kie_home.mkdir(parents=True, exist_ok=True)
    return kie_home / "tasks.jsonl"


def append(rec: dict) -> None:
    """Append a record to the ledger."""
    with _ledger_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def update(task_id: str, **kv: Any) -> None:
    """Update all rows matching taskId by merging kv. Rewrites the file."""
    path = _ledger_path()
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            updated.append(line)
            continue
        if row.get("taskId") == task_id:
            row.update(kv)
        updated.append(json.dumps(row, ensure_ascii=False))
    path.write_text("\n".join(updated) + "\n" if updated else "", encoding="utf-8")


def rows(limit: int | None = None, state: str | None = None) -> list[dict]:
    """Return records newest-first, optionally filtered by state and capped at limit."""
    path = _ledger_path()
    if not path.exists():
        return []
    all_rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            all_rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # newest-first (JSONL is append-order, so reverse)
    all_rows.reverse()

    if state is not None:
        all_rows = [r for r in all_rows if r.get("state") == state]

    if limit is not None:
        all_rows = all_rows[:limit]

    return all_rows
