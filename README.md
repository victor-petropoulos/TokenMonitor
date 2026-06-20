# TokenMonitor

CLI tool to monitor **Cursor agent token usage** and estimate **static config overhead** (MCPs, skills, rules) so you can reclaim context window headroom.

## What it does

| Layer | Accuracy | Source |
|-------|----------|--------|
| Live usage | **Exact** | Cursor hooks (`input_tokens`, cache fields) |
| Static scan | **Estimate** | tiktoken over `~/.cursor` config files |
| diff / simulate | **Static delta** | Before/after scan snapshots |

## Install

```bash
cd TokenMonitor
pip install -e .
# or: uv pip install -e .

token-monitor install-hook
```

Reload Cursor (**Developer: Reload Window**), send an agent message, then:

```bash
token-monitor report
token-monitor scan
token-monitor doctor
```

## Commands

```bash
token-monitor scan                    # Global static breakdown
token-monitor scan --save before.json # Snapshot for diff
token-monitor report                  # Latest turn (exact tokens + headroom)
token-monitor history --last 20       # Recent turns (cold vs warm)
token-monitor top-offenders           # Ranked trim candidates
token-monitor simulate --disable github,sonarqube
token-monitor diff before.json        # Compare snapshot to now
token-monitor doctor                  # Hook + DB health check
token-monitor install-hook            # Wire up ~/.cursor/hooks.json
```

## Trim-prove workflow

1. `token-monitor scan --save before.json`
2. Disable unused MCP servers in **Cursor Settings → MCP**
3. `token-monitor scan --save after.json && token-monitor diff before.json`
4. Start a **new agent session** → `token-monitor report` (compare cold-turn `input_tokens`)

## What to trim first

1. **MCP servers** — largest lever; each enabled server injects tool JSON schemas every turn
2. **Marketplace plugins** — plugin skills add metadata to every turn
3. **Rules** — set `alwaysApply: false` or scope with `globs`
4. **sessionStart hooks** — may inject extra context each session

## Data locations

- `~/.cursor/token-monitor/usage.db` — recorded hook events
- `~/.cursor/token-monitor/baselines/` — scan snapshots
- `~/.cursor/token-monitor/hook-failures.jsonl` — hook errors (fail-open)
- `~/.cursor/token-monitor/hook.env` — Python path for hook (written by install-hook)

## Limitations

- No per-category breakdown matching Cursor's internal Context Usage tray
- Static estimates ≠ exact `input_tokens` (system prompt, conversation, tool results are unattributed)
- `simulate` is static MCP schema math only — verify with a real agent turn after changes
- Records on `stop` hook only (deduped by `generation_id`)

## Development

```bash
pip install -e ".[dev]"
pytest
```
