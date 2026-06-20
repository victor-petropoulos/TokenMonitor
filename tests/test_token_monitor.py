from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from token_monitor.hook_record import record_from_stdin
from token_monitor.scanner import TokenCounter, diff_scans, simulate_disable, ScanResult, _rule_always_apply
from token_monitor.store import get_latest, insert_event


def test_token_counter():
    counter = TokenCounter("cl100k_base")
    assert counter.count("hello world") > 0
    assert counter.count("") == 0


def test_insert_event_dedup_stop_only(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    payload_stop = {
        "hook_event_name": "stop",
        "generation_id": "gen-1",
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_read_tokens": 800,
        "cache_write_tokens": 0,
    }
    payload_after = {**payload_stop, "hook_event_name": "afterAgentResponse"}

    assert insert_event(payload_after, db_path=db) is False
    assert insert_event(payload_stop, db_path=db) is True
    assert insert_event(payload_stop, db_path=db) is False  # duplicate generation_id

    conn = sqlite3.connect(db)
    count = conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
    conn.close()
    assert count == 1


def test_hook_record_stdin(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("TOKEN_MONITOR_TEST_DB", str(db))

    import token_monitor.store as store_mod

    original = store_mod.usage_db_path
    monkeypatch.setattr(store_mod, "usage_db_path", lambda: db)

    payload = json.dumps(
        {
            "hook_event_name": "stop",
            "generation_id": "gen-hook",
            "input_tokens": 5000,
        }
    )
    import io
    import sys

    sys.stdin = io.StringIO(payload)
    record_from_stdin()
    event = get_latest(db_path=db)
    assert event is not None
    assert event.input_tokens == 5000

    monkeypatch.setattr(store_mod, "usage_db_path", original)


def test_rule_always_apply():
    always_on = "---\nalwaysApply: true\n---\n# Rule body"
    scoped = "---\nalwaysApply: false\n---\n# Scoped rule"
    assert _rule_always_apply(always_on) is True
    assert _rule_always_apply(scoped) is False
    assert _rule_always_apply("# no frontmatter") is True


def test_simulate_disable_mcp():
    result = ScanResult(
        encoding="cl100k_base",
        scanned_at="2026-01-01T00:00:00Z",
        mcp_servers=[],
    )
    from token_monitor.scanner import McpServerEstimate

    result.mcp_servers = [
        McpServerEstimate("user-github", "github", True, 10, 5000, 10000),
        McpServerEstimate("user-sonarqube", "sonarqube", True, 5, 3000, 6000),
    ]
    sim = simulate_disable(result, ["github"])
    assert len(sim.mcp_servers) == 1
    assert sim.mcp_servers[0].config_name == "sonarqube"


def test_diff_scans():
    a = ScanResult(encoding="cl100k_base", scanned_at="t")
    b = ScanResult(encoding="cl100k_base", scanned_at="t")
    # Monkeypatch categories via mcp_servers
    from token_monitor.scanner import McpServerEstimate

    a.mcp_servers = [McpServerEstimate("s1", "s1", True, 1, 1000, 0)]
    b.mcp_servers = [McpServerEstimate("s1", "s1", True, 1, 400, 0)]
    deltas = diff_scans(a, b)
    assert deltas["mcp"] == 600
