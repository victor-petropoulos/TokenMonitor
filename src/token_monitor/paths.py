"""Resolve Cursor and TokenMonitor data paths."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONTEXT_WINDOW = 200_000


def cursor_dir() -> Path:
    return Path(os.environ.get("CURSOR_HOME", Path.home() / ".cursor")).expanduser()


def data_dir() -> Path:
    return cursor_dir() / "token-monitor"


def usage_db_path() -> Path:
    return data_dir() / "usage.db"


def baselines_dir() -> Path:
    return data_dir() / "baselines"


def hook_failures_path() -> Path:
    return data_dir() / "hook-failures.jsonl"


def hooks_json_path() -> Path:
    return cursor_dir() / "hooks.json"


def mcp_json_path() -> Path:
    return cursor_dir() / "mcp.json"


def rules_dir() -> Path:
    return cursor_dir() / "rules"


def user_skills_dir() -> Path:
    return cursor_dir() / "skills-cursor"


def plugin_skills_dir() -> Path:
    return cursor_dir() / "plugins" / "cache"


def projects_dir() -> Path:
    return cursor_dir() / "projects"


def workspace_to_project_slug(workspace: Path) -> str:
    """Map a workspace path to Cursor's project cache folder name."""
    resolved = workspace.expanduser().resolve()
    return str(resolved).replace("/", "-").lstrip("-")


def project_mcps_dir(workspace: Path | None = None) -> Path | None:
    if workspace is None:
        return None
    slug = workspace_to_project_slug(workspace)
    path = projects_dir() / slug / "mcps"
    return path if path.is_dir() else None


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    baselines_dir().mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def is_safe_under(base: Path, candidate: Path) -> bool:
    """Return True if resolved candidate is under base (path traversal guard)."""
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False
