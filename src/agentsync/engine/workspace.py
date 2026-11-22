"""Unified project workspace context & single-source instruction synchronizer (`.agentsync/workspace.yaml`)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from agentsync.engine.merge import merge_markdown_texts
from agentsync.utils.atomic_io import atomic_write_text

WORKSPACE_INSTRUCTION_FILES: dict[str, str] = {
    "AGENTS.md": "codex",
    "CLAUDE.md": "claude-code",
    "GEMINI.md": "antigravity",
    ".rules": "zed",
    ".github/copilot-instructions.md": "vscode-copilot",
}


class WorkspaceManifest(BaseModel):
    """Single-source project context descriptor persisted at `.agentsync/workspace.yaml`."""

    schema_version: int = 1
    name: str
    description: str = ""
    primary_instruction_file: str = "AGENTS.md"
    sync_targets: list[str] = Field(
        default_factory=lambda: ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".rules"]
    )
    shared_memory_file: str = ".agentsync/memory.md"
    default_agent: str = "codex"


class WorkspaceSyncResult(BaseModel):
    project_path: str
    manifest_path: str
    updated_files: list[str] = Field(default_factory=list)
    unchanged_files: list[str] = Field(default_factory=list)
    dry_run: bool = False


class WorkspaceEngine:
    """Keeps project-level instructions and shared context aligned across every AI agent."""

    def manifest_path(self, project_root: Path) -> Path:
        dot_cfg = project_root / ".agentsync" / "workspace.yaml"
        if dot_cfg.exists():
            return dot_cfg
        legacy = project_root / "workspace.yaml"
        if legacy.exists():
            return legacy
        return dot_cfg

    def init_workspace(
        self,
        project_root: Path,
        *,
        name: str | None = None,
        targets: list[str] | None = None,
        dry_run: bool = False,
    ) -> tuple[WorkspaceManifest, Path]:
        mpath = self.manifest_path(project_root)
        manifest = WorkspaceManifest(
            name=name or project_root.name,
            sync_targets=targets or ["AGENTS.md", "CLAUDE.md", "GEMINI.md", ".rules"],
        )
        if not dry_run:
            dumped = yaml.safe_dump(manifest.model_dump(), sort_keys=False, allow_unicode=True)
            atomic_write_text(mpath, dumped)
            mem_file = project_root / manifest.shared_memory_file
            if not mem_file.exists():
                atomic_write_text(
                    mem_file,
                    f"# {manifest.name} - Shared Agent Context\n\n## Architecture & Decisions\n\n",
                )
        return manifest, mpath

    def load_manifest(self, project_root: Path) -> WorkspaceManifest:
        mpath = self.manifest_path(project_root)
        if not mpath.exists():
            return WorkspaceManifest(name=project_root.name)
        data = yaml.safe_load(mpath.read_text(encoding="utf-8-sig")) or {}
        if not isinstance(data, dict):
            return WorkspaceManifest(name=project_root.name)
        data.setdefault("name", project_root.name)
        return WorkspaceManifest.model_validate(data)

    def sync_workspace(self, project_root: Path, *, dry_run: bool = False) -> WorkspaceSyncResult:
        """Merge all present project instruction files without duplication and write back to all sync_targets."""
        manifest = self.load_manifest(project_root)
        mpath = self.manifest_path(project_root)
        targets = list(dict.fromkeys([manifest.primary_instruction_file, *manifest.sync_targets]))

        present_texts: list[tuple[str, str, float]] = []
        for rel in targets:
            p = project_root / rel
            if p.exists() and p.is_file():
                txt = p.read_text(encoding="utf-8-sig", errors="replace").strip()
                if txt:
                    present_texts.append((rel, txt, p.stat().st_mtime))

        if not present_texts:
            return WorkspaceSyncResult(
                project_path=str(project_root),
                manifest_path=str(mpath),
                dry_run=dry_run,
            )

        # Newest modified file acts as conflict tie-breaker; unique headings from all files are merged
        present_texts.sort(key=lambda item: item[2], reverse=True)
        merged = present_texts[0][1]
        for _, other_txt, _ in present_texts[1:]:
            merged, _ = merge_markdown_texts(merged, other_txt, primary_wins=True)
        if not merged.endswith("\n"):
            merged += "\n"

        updated: list[str] = []
        unchanged: list[str] = []
        for rel in targets:
            p = project_root / rel
            current = p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""
            if current.replace("\r\n", "\n") == merged:
                unchanged.append(rel)
            else:
                updated.append(rel)
                if not dry_run:
                    atomic_write_text(p, merged)

        return WorkspaceSyncResult(
            project_path=str(project_root),
            manifest_path=str(mpath),
            updated_files=updated,
            unchanged_files=unchanged,
            dry_run=dry_run,
        )
