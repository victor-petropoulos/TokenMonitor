"""Record Cursor hook usage payloads to SQLite (stdin JSON)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from token_monitor.paths import ensure_data_dir, hook_failures_path
from token_monitor.store import insert_event


def log_failure(error: str, payload_preview: str = "") -> None:
    ensure_data_dir()
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "error": error,
        "preview": payload_preview[:500],
    }
    with hook_failures_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def record_from_stdin() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        log_failure(f"invalid json: {exc}", raw)
        return 0

    try:
        insert_event(payload)
    except Exception as exc:  # noqa: BLE001 — fail-open hook
        log_failure(str(exc), raw)
    return 0


def main() -> None:
    raise SystemExit(record_from_stdin())


if __name__ == "__main__":
    main()
