"""Canonical domain models with deterministic content hashing and chat fingerprinting."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from agentsync.config import EntityType
from agentsync.utils.paths import normalize_path, path_dedup_key
from agentsync.utils.secrets import mask_mapping

McpTransport = Literal["stdio", "http", "sse"]
ActionKind = Literal["CREATE", "UPDATE", "DELETE", "MERGE", "LINK", "SKIP", "CONFLICT", "REPAIR"]


def _canonical_json_hash(obj: Any) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_text_for_hash(text: str) -> str:
    """Normalize line endings and trailing spaces for deterministic cross-tool hashing."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip()


# --- Skills ---


_SIMPLE_FM_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*([^:#\[\]{}'\"|>!*&][^\r\n]*)$")
_WS_COLLAPSE_RE = re.compile(r"\s+")


def parse_skill_md(content: str, folder_name: str) -> tuple[dict[str, Any], str, bool]:
    """Parse SKILL.md into `(frontmatter_dict, body, had_frontmatter)` with fast-path for flat frontmatter."""
    cleaned = content.lstrip("\ufeff")
    if cleaned.startswith("---"):
        lines = cleaned.splitlines()
        end_idx = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                end_idx = idx
                break
        if end_idx is not None:
            fm_lines = lines[1:end_idx]
            body = "\n".join(lines[end_idx + 1 :]).lstrip("\r\n")
            # Fast-path O(N) parse for simple flat key: value frontmatter (avoids slow yaml.safe_load for 95% of skills)
            fast_dict: dict[str, Any] = {}
            fast_ok = True
            for ln in fm_lines:
                s = ln.strip()
                if not s or s.startswith("#"):
                    continue
                m = _SIMPLE_FM_LINE_RE.match(ln)
                if not m:
                    fast_ok = False
                    break
                val = m.group(2).strip()
                if val.lower() in ("true", "false", "null", "~") or val[0].isdigit():
                    fast_ok = False
                    break
                fast_dict[m.group(1)] = val
            if fast_ok and fast_dict:
                return fast_dict, body, True
            fm_text = "\n".join(fm_lines)
            try:
                parsed = yaml.safe_load(fm_text)
                if isinstance(parsed, dict):
                    return parsed, body, True
            except yaml.YAMLError:
                pass
    return {}, cleaned, False


def infer_skill_description(body: str, folder_name: str) -> str:
    """Extract a clean one-line description from a SKILL.md body when frontmatter is missing."""
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("```") or s.startswith("---"):
            continue
        return s[:240]
    return f"Skill instructions for {folder_name}."


def ensure_skill_frontmatter(content: str, folder_name: str) -> str:
    """Ensure SKILL.md begins with valid YAML frontmatter containing `name` and `description`."""
    fm, body, had_fm = parse_skill_md(content, folder_name)
    if had_fm and fm.get("name") and fm.get("description"):
        return content.lstrip("\ufeff")
    fm = dict(fm)
    fm.setdefault("name", folder_name)
    if not fm.get("description"):
        fm["description"] = infer_skill_description(body, folder_name)
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n\n{body.lstrip()}"


def compute_skill_hash(folder_name: str, files: dict[str, bytes]) -> str:
    """Deterministic content hash for a skill bundle (invariant to synthesized frontmatter)."""
    entries: list[list[str]] = []
    for rel in sorted(files.keys()):
        data = files[rel]
        if rel.lower() == "skill.md":
            text = data.decode("utf-8", errors="replace")
            fm, body, _ = parse_skill_md(text, folder_name)
            norm_fm: dict[str, Any] = {
                "name": str(fm.get("name") or folder_name).strip(),
                "description": str(
                    fm.get("description") or infer_skill_description(body, folder_name)
                ).strip(),
            }
            for k, v in sorted(fm.items()):
                if k not in ("name", "description"):
                    norm_fm[str(k)] = v
            norm_payload = {"fm": norm_fm, "body": normalize_text_for_hash(body)}
            entries.append([rel.lower(), _canonical_json_hash(norm_payload)])
        else:
            entries.append([rel.replace("\\", "/"), hashlib.sha256(data).hexdigest()])
    return _canonical_json_hash(entries)


class SkillItem(BaseModel):
    name: str
    frontmatter_name: str = ""
    description: str = ""
    files: dict[str, bytes] = Field(default_factory=dict, exclude=True)
    file_hashes: dict[str, str] = Field(default_factory=dict)
    content_hash: str = ""
    updated_at_ms: int = 0
    source_tool: str = "codex"
    synthesized_frontmatter: bool = False

    def compute_hashes(self) -> SkillItem:
        self.file_hashes = {
            rel: hashlib.sha256(data).hexdigest() for rel, data in sorted(self.files.items())
        }
        self.content_hash = compute_skill_hash(self.name, self.files)
        return self


# --- MCP Servers ---


class McpVariable(BaseModel):
    name: str
    title: str = ""
    description: str = ""


class McpServer(BaseModel):
    name: str
    transport: McpTransport = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False
    variables: list[McpVariable] = Field(default_factory=list)
    extra_fields: dict[str, Any] = Field(default_factory=dict)
    portable: bool = True
    skip_reason: str | None = None
    source_tool: str = "codex"
    updated_at_ms: int = 0
    content_hash: str = ""

    def compute_hash(self) -> McpServer:
        # Infer referenced ${ENV_VAR} placeholders uniformly across all adapters
        existing_var_names = {v.name for v in self.variables}
        inferred_names: set[str] = set()
        for val in (*self.headers.values(), *self.env.values(), self.url or ""):
            for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", str(val)):
                inferred_names.add(m.group(1))
        for vname in sorted(inferred_names - existing_var_names):
            self.variables.append(McpVariable(name=vname, title=vname))

        norm_cwd = path_dedup_key(self.cwd) if self.cwd else None
        payload = {
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": self.args,
            "env": dict(sorted(self.env.items())),
            "cwd": norm_cwd,
            "url": self.url,
            "headers": dict(sorted(self.headers.items())),
            "disabled": self.disabled,
            "variables": sorted(v.name for v in self.variables),
        }
        self.content_hash = _canonical_json_hash(payload)
        return self

    def masked_summary(self) -> dict[str, Any]:
        return mask_mapping(
            {
                "name": self.name,
                "transport": self.transport,
                "command": self.command,
                "args": self.args,
                "env": self.env,
                "cwd": self.cwd,
                "url": self.url,
                "headers": self.headers,
                "disabled": self.disabled,
                "variables": [v.name for v in self.variables],
                "portable": self.portable,
                "skip_reason": self.skip_reason,
            }
        )


# --- Plugins ---


class PluginItem(BaseModel):
    name: str
    display_name: str = ""
    version: str = "1.0.0"
    description: str = ""
    enabled: bool = True
    marketplace: str | None = None
    skills: dict[str, SkillItem] = Field(default_factory=dict)
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)
    rules: dict[str, str] = Field(default_factory=dict)
    portable: bool = True
    skip_reason: str | None = None
    source_tool: str = "codex"
    updated_at_ms: int = 0
    content_hash: str = ""

    def compute_hash(self) -> PluginItem:
        payload = {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "version": self.version,
            "description": self.description,
            "enabled": self.enabled,
            "skills": {k: v.content_hash for k, v in sorted(self.skills.items())},
            "mcp_servers": {
                k: v.content_hash for k, v in sorted(self.mcp_servers.items()) if v.portable
            },
            "rules": {k: normalize_text_for_hash(v) for k, v in sorted(self.rules.items())},
        }
        self.content_hash = _canonical_json_hash(payload)
        return self


# --- Instructions & Rules ---


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def strip_rule_frontmatter(markdown: str) -> str:
    """Remove YAML rule frontmatter (e.g. `trigger: always_on`) for canonical comparison."""
    cleaned = markdown.lstrip("\ufeff")
    if cleaned.startswith("---"):
        lines = cleaned.splitlines()
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                return "\n".join(lines[idx + 1 :]).lstrip("\r\n")
    return cleaned


class InstructionSection(BaseModel):
    heading_level: int = 0
    heading_title: str = ""
    normalized_heading: str = ""
    body: str = ""
    section_hash: str = ""

    def compute_hash(self) -> InstructionSection:
        self.normalized_heading = re.sub(r"\s+", " ", self.heading_title.strip().lower())
        self.section_hash = _canonical_json_hash(
            {
                "level": self.heading_level,
                "heading": self.normalized_heading,
                "body": normalize_text_for_hash(self.body),
            }
        )
        return self


def parse_markdown_sections(raw_markdown: str) -> list[InstructionSection]:
    """Split Markdown into deduplicatable sections by headings (ignoring headings inside code fences)."""
    body_text = strip_rule_frontmatter(raw_markdown).replace("\r\n", "\n").replace("\r", "\n")
    sections: list[InstructionSection] = []
    cur_level = 0
    cur_title = ""
    cur_lines: list[str] = []
    in_fence = False

    def _flush() -> None:
        text = "\n".join(cur_lines).strip()
        if cur_title or text:
            sections.append(
                InstructionSection(
                    heading_level=cur_level,
                    heading_title=cur_title,
                    body=text,
                ).compute_hash()
            )

    for line in body_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            cur_lines.append(line)
            continue
        if not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                _flush()
                cur_level = len(m.group(1))
                cur_title = m.group(2).strip()
                cur_lines = []
                continue
        cur_lines.append(line)

    _flush()
    return sections


def render_markdown_sections(sections: list[InstructionSection]) -> str:
    """Render canonical sections back into normalized Markdown."""
    blocks: list[str] = []
    for sec in sections:
        parts: list[str] = []
        if sec.heading_level > 0 and sec.heading_title:
            parts.append(f"{'#' * sec.heading_level} {sec.heading_title}")
        if sec.body.strip():
            parts.append(sec.body.strip())
        if parts:
            blocks.append("\n\n".join(parts))
    return ("\n\n".join(blocks).strip() + "\n") if blocks else ""


class InstructionDoc(BaseModel):
    scope: Literal["global", "project"]
    project_key: str | None = None
    project_path: str | None = None
    sections: list[InstructionSection] = Field(default_factory=list)
    raw_text: str = ""
    content_hash: str = ""
    updated_at_ms: int = 0
    source_files: list[str] = Field(default_factory=list)
    source_tool: str = "codex"

    @classmethod
    def from_markdown(
        cls,
        raw_markdown: str,
        scope: Literal["global", "project"],
        source_tool: str,
        updated_at_ms: int = 0,
        project_key: str | None = None,
        project_path: str | None = None,
        source_files: list[str] | None = None,
    ) -> InstructionDoc:
        sections = parse_markdown_sections(raw_markdown)
        rendered = render_markdown_sections(sections)
        doc = cls(
            scope=scope,
            project_key=project_key,
            project_path=project_path,
            sections=sections,
            raw_text=rendered,
            updated_at_ms=updated_at_ms,
            source_files=source_files or [],
            source_tool=source_tool,
        )
        doc.content_hash = _canonical_json_hash(normalize_text_for_hash(rendered))
        return doc


# --- Projects / Workspaces ---


class ProjectItem(BaseModel):
    canonical_key: str
    display_path: str
    name: str
    native_ids_by_tool: dict[str, list[str]] = Field(default_factory=dict)
    secondary_ids_by_tool: dict[str, list[str]] = Field(default_factory=dict)
    is_git: bool = False
    default_branch: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0

    @classmethod
    def from_path(
        cls,
        raw_path: str,
        name: str | None = None,
        **kwargs: Any,
    ) -> ProjectItem:
        display = normalize_path(raw_path)
        key = path_dedup_key(raw_path)
        inferred_name = name or (display.rstrip("/").split("/")[-1] if display else "workspace")
        return cls(canonical_key=key, display_path=display, name=inferred_name, **kwargs)

    def primary_id(self, tool_name: str) -> str | None:
        ids = self.native_ids_by_tool.get(tool_name)
        return ids[0] if ids else None

    def secondary_id(self, tool_name: str) -> str | None:
        ids = self.secondary_ids_by_tool.get(tool_name)
        return ids[0] if ids else None

    def duplicate_count(self, tool_name: str) -> int:
        ids = self.native_ids_by_tool.get(tool_name) or []
        return max(0, len(ids) - 1)


# --- Chats / Sessions ---


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    text: str
    timestamp_ms: int = 0
    turn_id: str | None = None


def _normalize_msg_for_fingerprint(text: str) -> str:
    collapsed = _WS_COLLAPSE_RE.sub(" ", text.replace("\r\n", "\n")).strip().lower()
    return collapsed[:500]


def compute_chat_fingerprint(
    project_key: str | None,
    messages: list[ChatMessage],
    title: str = "",
    max_messages: int = 4,
) -> str:
    """Deterministic SHA-256 fingerprint for deduplicating unmapped chats across tools."""
    norm_proj = (project_key or "").strip().lower()
    picked: list[list[str]] = []
    for msg in messages:
        if msg.role in ("user", "assistant"):
            norm = _normalize_msg_for_fingerprint(msg.text)
            if norm:
                picked.append([msg.role, norm])
            if len(picked) >= max_messages:
                break
    if not picked:
        picked.append(["title", _normalize_msg_for_fingerprint(title)])
    return _canonical_json_hash({"project": norm_proj, "head": picked})


def compute_chat_content_hash(
    title: str,
    project_key: str | None,
    messages: list[ChatMessage],
) -> str:
    """Deterministic canonical hash of chat content (independent of tool IDs/timestamps)."""
    norm_msgs = [
        [m.role, normalize_text_for_hash(m.text)]
        for m in messages
        if m.role in ("user", "assistant") and m.text.strip()
    ]
    return _canonical_json_hash(
        {
            "project": (project_key or "").strip().lower(),
            "title": re.sub(r"\s+", " ", title.strip()),
            "messages": norm_msgs,
        }
    )


class ChatSession(BaseModel):
    tool: str
    native_id: str
    title: str = ""
    project_key: str | None = None
    project_path: str | None = None
    native_project_id: str | None = None
    created_at_ms: int = 0
    updated_at_ms: int = 0
    is_subagent: bool = False
    parent_id: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    fingerprint: str = ""
    content_hash: str = ""

    def compute_hashes(self) -> ChatSession:
        if self.project_path and not self.project_key:
            self.project_key = path_dedup_key(self.project_path)
        self.fingerprint = compute_chat_fingerprint(
            self.project_key, self.messages, title=self.title
        )
        self.content_hash = compute_chat_content_hash(
            self.title, self.project_key, self.messages
        )
        return self


# --- Sync Plan, Diagnostics, and Reporting ---


class SyncAction(BaseModel):
    category: EntityType
    action: ActionKind
    target_tool: str
    entity_id: str
    summary: str
    risk_level: Literal["low", "medium", "high"] = "low"
    details: dict[str, Any] = Field(default_factory=dict)

    def masked(self) -> SyncAction:
        return self.model_copy(update={"details": mask_mapping(self.details)})


class SyncPlan(BaseModel):
    mode: Literal["bootstrap", "sync", "repair", "patch"]
    dry_run: bool = False
    actions: list[SyncAction] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    backup_id: str | None = None
    ledger_path: str | None = None

    @property
    def mutating_actions(self) -> list[SyncAction]:
        return [a for a in self.actions if a.action not in ("SKIP",)]

    @property
    def skipped_actions(self) -> list[SyncAction]:
        return [a for a in self.actions if a.action == "SKIP"]

    @property
    def conflict_actions(self) -> list[SyncAction]:
        return [a for a in self.actions if a.action == "CONFLICT"]

    @property
    def is_empty(self) -> bool:
        return len(self.mutating_actions) == 0

    def add(self, action: SyncAction) -> None:
        from agentsync.utils.selectors import classify_action_risk

        risk = action.risk_level
        if risk == "low":
            risk = classify_action_risk(action.category, action.action, action.entity_id, action.summary)
        self.actions.append(action.model_copy(update={"risk_level": risk}).masked())


class DoctorIssue(BaseModel):
    tool: str
    severity: Literal["error", "warning", "info"]
    code: str
    path: str = ""
    message: str
    fixable: bool = False


class RepairResult(BaseModel):
    tool: str
    fixed_count: int = 0
    quarantined_files: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)
