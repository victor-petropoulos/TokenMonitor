"""SQLite storage for hook-recorded usage events."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from token_monitor.paths import ensure_data_dir, usage_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  event TEXT NOT NULL,
  conversation_id TEXT,
  generation_id TEXT UNIQUE,
  model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  workspace_roots TEXT,
  transcript_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_conversation ON usage_events(conversation_id);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_events(ts);
"""


@dataclass
class UsageEvent:
    id: int
    ts: str
    event: str
    conversation_id: str | None
    generation_id: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    workspace_roots: list[str]
    transcript_path: str | None

    @property
    def new_tokens(self) -> int | None:
        if self.input_tokens is None or self.cache_read_tokens is None:
            return None
        return max(0, self.input_tokens - self.cache_read_tokens)

    @property
    def is_cold_turn(self) -> bool:
        if self.cache_write_tokens is None:
            return False
        return self.cache_write_tokens > 0 and (
            self.cache_read_tokens is None or self.cache_read_tokens < self.cache_write_tokens
        )


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    ensure_data_dir()
    path = db_path or usage_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _row_to_event(row: sqlite3.Row) -> UsageEvent:
    roots_raw = row["workspace_roots"]
    roots: list[str] = []
    if roots_raw:
        try:
            roots = json.loads(roots_raw)
        except json.JSONDecodeError:
            roots = []
    return UsageEvent(
        id=row["id"],
        ts=row["ts"],
        event=row["event"],
        conversation_id=row["conversation_id"],
        generation_id=row["generation_id"],
        model=row["model"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cache_read_tokens=row["cache_read_tokens"],
        cache_write_tokens=row["cache_write_tokens"],
        workspace_roots=roots,
        transcript_path=row["transcript_path"],
    )


def insert_event(payload: dict[str, Any], *, db_path: Path | None = None) -> bool:
    """Insert a usage event. Returns True if inserted, False if duplicate or skipped."""
    event_name = payload.get("hook_event_name") or payload.get("event") or ""
    if event_name != "stop":
        return False

    generation_id = payload.get("generation_id")
    if not generation_id:
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    workspace_roots = payload.get("workspace_roots") or []
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO usage_events (
              ts, event, conversation_id, generation_id, model,
              input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
              workspace_roots, transcript_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                event_name,
                payload.get("conversation_id") or payload.get("session_id"),
                generation_id,
                payload.get("model"),
                payload.get("input_tokens"),
                payload.get("output_tokens"),
                payload.get("cache_read_tokens"),
                payload.get("cache_write_tokens"),
                json.dumps(workspace_roots),
                payload.get("transcript_path"),
            ),
        )
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def get_latest(*, db_path: Path | None = None) -> UsageEvent | None:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM usage_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _row_to_event(row) if row else None
    finally:
        conn.close()


def get_history(limit: int = 20, *, db_path: Path | None = None) -> list[UsageEvent]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM usage_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_event(r) for r in rows]
    finally:
        conn.close()


def get_last_event_age_hours(*, db_path: Path | None = None) -> float | None:
    event = get_latest(db_path=db_path)
    if not event:
        return None
    try:
        ts = datetime.fromisoformat(event.ts.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts
        return delta.total_seconds() / 3600
    except ValueError:
        return None
