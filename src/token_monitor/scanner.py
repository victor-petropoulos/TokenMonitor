"""Scan global Cursor static config and estimate token overhead."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import tiktoken

from token_monitor.paths import (
    cursor_dir,
    is_safe_under,
    mcp_json_path,
    plugin_skills_dir,
    project_mcps_dir,
    projects_dir,
    rules_dir,
    user_skills_dir,
)

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class ItemEstimate:
    path: str
    listed_tokens: int = 0
    body_tokens: int = 0
    chars: int = 0

    @property
    def total_tokens(self) -> int:
        return self.listed_tokens + self.body_tokens


@dataclass
class McpServerEstimate:
    server_id: str
    config_name: str | None
    enabled: bool
    tool_count: int
    tokens: int
    chars: int
    top_tools: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class CategorySummary:
    name: str
    item_count: int
    listed_tokens: int
    body_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.listed_tokens + self.body_tokens


@dataclass
class ScanResult:
    encoding: str
    scanned_at: str
    rules: list[ItemEstimate] = field(default_factory=list)
    user_skills: list[ItemEstimate] = field(default_factory=list)
    plugin_skills: list[ItemEstimate] = field(default_factory=list)
    mcp_servers: list[McpServerEstimate] = field(default_factory=list)

    def categories(self) -> list[CategorySummary]:
        return [
            CategorySummary(
                "rules",
                len(self.rules),
                sum(r.listed_tokens for r in self.rules),
                sum(r.body_tokens for r in self.rules),
            ),
            CategorySummary(
                "user_skills",
                len(self.user_skills),
                sum(s.listed_tokens for s in self.user_skills),
                sum(s.body_tokens for s in self.user_skills),
            ),
            CategorySummary(
                "plugin_skills",
                len(self.plugin_skills),
                sum(s.listed_tokens for s in self.plugin_skills),
                sum(s.body_tokens for s in self.plugin_skills),
            ),
            CategorySummary(
                "mcp",
                len(self.mcp_servers),
                sum(m.tokens for m in self.mcp_servers),
                0,
            ),
        ]

    def total_listed_tokens(self) -> int:
        return sum(c.listed_tokens + c.body_tokens for c in self.categories())

    def session_overhead_tokens(self, *, enabled_mcp_only: bool = True) -> int:
        """Estimate always-on context (skill metadata + rules + MCP schemas)."""
        rules = sum(r.listed_tokens for r in self.rules)
        user_listed = sum(s.listed_tokens for s in self.user_skills)
        plugin_listed = sum(s.listed_tokens for s in self.plugin_skills)
        if enabled_mcp_only:
            mcp = sum(m.tokens for m in self.mcp_servers if m.enabled)
        else:
            mcp = sum(m.tokens for m in self.mcp_servers)
        return rules + user_listed + plugin_listed + mcp

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ScanResult:
        return cls(
            encoding=data["encoding"],
            scanned_at=data["scanned_at"],
            rules=[ItemEstimate(**r) for r in data.get("rules", [])],
            user_skills=[ItemEstimate(**s) for s in data.get("user_skills", [])],
            plugin_skills=[ItemEstimate(**s) for s in data.get("plugin_skills", [])],
            mcp_servers=[McpServerEstimate(**m) for m in data.get("mcp_servers", [])],
        )


class TokenCounter:
    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        self.encoding_name = encoding_name
        self._enc = tiktoken.get_encoding(encoding_name)

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self._enc.encode(text))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_skill_metadata(text: str) -> str:
    """Extract name + description from SKILL.md frontmatter for listed_tokens estimate."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return text[:200]
    fm = match.group(1)
    name = ""
    description = ""
    for line in fm.splitlines():
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip('"')
        elif line.startswith("description:"):
            description = line.split(":", 1)[1].strip().strip('"')
    return f"{name}: {description}".strip(": ")


def _scan_rules(counter: TokenCounter, base: Path) -> list[ItemEstimate]:
    items: list[ItemEstimate] = []
    if not base.is_dir():
        return items
    for path in sorted(base.glob("*.mdc")):
        if not is_safe_under(base, path):
            continue
        text = _read_text(path)
        tokens = counter.count(text)
        items.append(
            ItemEstimate(
                path=str(path),
                listed_tokens=tokens,
                body_tokens=0,
                chars=len(text),
            )
        )
    return items


def _scan_skills(counter: TokenCounter, base: Path) -> list[ItemEstimate]:
    items: list[ItemEstimate] = []
    if not base.is_dir():
        return items
    for path in sorted(base.rglob("SKILL.md")):
        if not is_safe_under(base, path):
            continue
        text = _read_text(path)
        meta = _parse_skill_metadata(text)
        body = text
        if FRONTMATTER_RE.match(text):
            body = text[FRONTMATTER_RE.match(text).end() :]  # type: ignore[union-attr]
        items.append(
            ItemEstimate(
                path=str(path),
                listed_tokens=counter.count(meta),
                body_tokens=counter.count(body),
                chars=len(text),
            )
        )
    return items


def _load_mcp_config() -> tuple[dict[str, str], set[str]]:
    """Return mapping of normalized keys -> config name, and enabled server names."""
    path = mcp_json_path()
    if not path.is_file():
        return {}, set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, set()

    servers = data.get("mcpServers") or {}
    enabled: set[str] = set()
    aliases: dict[str, str] = {}
    for name in servers:
        enabled.add(name.lower())
        aliases[name.lower()] = name
        # plugin-foo-bar -> foo-bar style folder names
        slug = name.lower().replace("_", "-")
        aliases[slug] = name
        if name.startswith("plugin-"):
            aliases[name[7:].lower()] = name
    return aliases, enabled


def _match_config_name(server_folder: str, aliases: dict[str, str]) -> str | None:
    key = server_folder.lower()
    if key in aliases:
        return aliases[key]
    # user-github -> github
    if key.startswith("user-"):
        short = key[5:]
        if short in aliases:
            return aliases[short]
    if key.startswith("plugin-"):
        short = key[7:]
        if short in aliases:
            return aliases[short]
    return None


def _collect_mcp_tool_files(
    workspace: Path | None = None,
) -> dict[str, list[tuple[Path, str]]]:
    """Gather tool JSON files grouped by server folder name (one project cache per server)."""
    per_server: dict[str, list[list[tuple[Path, str]]]] = {}

    if workspace is not None:
        mcps_roots = [project_mcps_dir(workspace)] if project_mcps_dir(workspace) else []
    else:
        mcps_roots = []
        proj = projects_dir()
        if proj.is_dir():
            for project in proj.iterdir():
                mcps = project / "mcps"
                if mcps.is_dir():
                    mcps_roots.append(mcps)

    for mcps_root in mcps_roots:
        if mcps_root is None or not mcps_root.is_dir():
            continue
        for server_dir in mcps_root.iterdir():
            if not server_dir.is_dir():
                continue
            tools_dir = server_dir / "tools"
            if not tools_dir.is_dir():
                continue
            server_id = server_dir.name
            batch: list[tuple[Path, str]] = []
            for tool_file in tools_dir.glob("*.json"):
                if not is_safe_under(cursor_dir(), tool_file):
                    continue
                batch.append((tool_file, _read_text(tool_file)))
            if batch:
                per_server.setdefault(server_id, []).append(batch)

    grouped: dict[str, list[tuple[Path, str]]] = {}
    for server_id, batches in per_server.items():
        grouped[server_id] = max(batches, key=lambda b: sum(len(t) for _, t in b))
    return grouped


def _scan_mcp(
    counter: TokenCounter,
    workspace: Path | None = None,
) -> list[McpServerEstimate]:
    aliases, enabled_names = _load_mcp_config()
    grouped = _collect_mcp_tool_files(workspace)

    # Dedupe: keep largest tool set per server_id
    estimates: list[McpServerEstimate] = []
    for server_id, files in sorted(grouped.items()):
        tool_tokens: list[tuple[str, int]] = []
        total_chars = 0
        for path, text in files:
            tokens = counter.count(text)
            tool_tokens.append((path.name, tokens))
            total_chars += len(text)
        tool_tokens.sort(key=lambda x: x[1], reverse=True)
        config_name = _match_config_name(server_id, aliases)
        enabled = False
        if config_name:
            enabled = config_name.lower() in {n.lower() for n in enabled_names}
        elif server_id.lower() in {n.lower() for n in enabled_names}:
            enabled = True
            config_name = server_id

        estimates.append(
            McpServerEstimate(
                server_id=server_id,
                config_name=config_name,
                enabled=enabled,
                tool_count=len(files),
                tokens=sum(t for _, t in tool_tokens),
                chars=total_chars,
                top_tools=tool_tokens[:5],
            )
        )

    by_config: dict[str, McpServerEstimate] = {}
    for est in estimates:
        key = est.config_name or est.server_id
        existing = by_config.get(key)
        if existing is None or est.tokens > existing.tokens:
            by_config[key] = est
    return sorted(by_config.values(), key=lambda e: e.tokens, reverse=True)


def run_scan(
    encoding: str = "cl100k_base",
    workspace: Path | None = None,
) -> ScanResult:
    counter = TokenCounter(encoding)
    return ScanResult(
        encoding=encoding,
        scanned_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        rules=_scan_rules(counter, rules_dir()),
        user_skills=_scan_skills(counter, user_skills_dir()),
        plugin_skills=_scan_skills(counter, plugin_skills_dir()),
        mcp_servers=_scan_mcp(counter, workspace),
    )


def save_scan(result: ScanResult, path: Path, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; pass force=True to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")


def load_scan(path: Path) -> ScanResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ScanResult.from_dict(data)


def diff_scans(baseline: ScanResult, current: ScanResult) -> dict[str, int]:
    """Return token delta per category (baseline - current = savings)."""
    base_cats = {c.name: c.total_tokens for c in baseline.categories()}
    cur_cats = {c.name: c.total_tokens for c in current.categories()}
    all_names = set(base_cats) | set(cur_cats)
    return {name: base_cats.get(name, 0) - cur_cats.get(name, 0) for name in all_names}


def simulate_disable(
    result: ScanResult,
    disable_names: list[str],
) -> ScanResult:
    """Return a copy with matching MCP servers removed (static estimate only)."""
    names_lower = {n.lower() for n in disable_names}
    filtered = []
    for srv in result.mcp_servers:
        keys = {srv.server_id.lower(), (srv.config_name or "").lower()}
        if names_lower & keys:
            continue
        filtered.append(srv)
    return ScanResult(
        encoding=result.encoding,
        scanned_at=result.scanned_at,
        rules=result.rules,
        user_skills=result.user_skills,
        plugin_skills=result.plugin_skills,
        mcp_servers=filtered,
    )
