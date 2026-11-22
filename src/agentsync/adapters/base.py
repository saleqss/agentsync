"""Abstract ToolAdapter contract isolating every AI agent's storage format."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agentsync.config import EntityType, ToolConfig
from agentsync.models.canonical import (
    ChatSession,
    DoctorIssue,
    InstructionDoc,
    McpServer,
    PluginItem,
    ProjectItem,
    RepairResult,
    SkillItem,
)


class ToolCapabilities(BaseModel):
    """Declares which entity types a tool adapter can read and write safely."""

    skills: bool = False
    mcp: bool = False
    plugins: bool = False
    instructions: bool = False
    projects: bool = False
    chats: bool = False
    chat_write: bool = False
    plugin_write: bool = False
    project_names: bool = True

    def supports(self, entity: EntityType) -> bool:
        return bool(getattr(self, entity, False))

    def can_write(self, entity: EntityType) -> bool:
        if entity == "chats":
            return self.chats and self.chat_write
        if entity == "plugins":
            return self.plugins and self.plugin_write
        return self.supports(entity)


class ToolAdapter:
    """Base contract for all AI agent adapters (`Adapter <-> CanonicalModel <-> Adapter`)."""

    tool_name: str = ""
    display_name: str = ""
    capabilities: ToolCapabilities = ToolCapabilities()

    def __init__(self, tool_config: ToolConfig) -> None:
        self.tool_config = tool_config
        self.root_path = Path(tool_config.path) if tool_config.path else Path(".")
        self.extra_paths = {k: Path(v) for k, v in tool_config.extra_paths.items()}

    # --- Backup & watcher metadata ---

    def backup_targets(
        self,
    ) -> tuple[list[tuple[Path, Literal["file", "sqlite"]]], list[Path]]:
        """Return `(single_files, tracked_directories)` owned by this tool."""
        return [], []

    def should_skip_backup_file(self, root_dir: Path, file_path: Path) -> bool:
        if file_path.name.endswith(("-wal", "-shm", "-journal")):
            return True
        return False

    def is_sqlite_backup_file(self, root_dir: Path, file_path: Path) -> bool:
        return False

    def watch_roots(self) -> list[Path]:
        return [self.root_path] if self.root_path.exists() else []

    # --- Bootstrap clear ---

    def clear_managed_for_bootstrap(self) -> dict[str, list[str]]:
        """Clear managed skills, MCP servers, and plugins before clean bootstrap import."""
        return {"skills": [], "mcp": [], "plugins": []}

    # --- 1. Skills ---

    def read_skills(self) -> dict[str, SkillItem]:
        return {}

    def write_skill(self, skill: SkillItem) -> bool:
        return False

    def delete_skill(self, skill_name: str) -> bool:
        return False

    # --- 2. MCP Servers ---

    def read_mcp_servers(self) -> dict[str, McpServer]:
        return {}

    def write_mcp_servers(self, servers: dict[str, McpServer]) -> tuple[bool, list[str]]:
        return False, []

    # --- 3. Plugins ---

    def read_plugins(self) -> dict[str, PluginItem]:
        return {}

    def write_plugin(self, plugin: PluginItem) -> tuple[bool, list[str]]:
        return False, []

    def delete_plugin(self, plugin_name: str) -> bool:
        return False

    # --- 4. Instructions & Rules ---

    def read_global_instructions(self) -> InstructionDoc | None:
        return None

    def write_global_instructions(self, doc: InstructionDoc) -> tuple[bool, list[str]]:
        return False, []

    def read_project_instructions(self, project_path: Path) -> InstructionDoc | None:
        return None

    def write_project_instructions(
        self, project_path: Path, doc: InstructionDoc
    ) -> tuple[bool, list[str]]:
        return False, []

    # --- 5. Projects / Workspaces ---

    def read_projects(self) -> dict[str, ProjectItem]:
        return {}

    def write_projects(
        self, projects: dict[str, ProjectItem]
    ) -> tuple[bool, dict[str, ProjectItem]]:
        return False, projects

    # --- 6. Chats / Sessions ---

    def read_chats(
        self,
        projects_by_key: dict[str, ProjectItem],
        include_subagents: bool = False,
    ) -> list[ChatSession]:
        return []

    def write_chat(
        self,
        chat: ChatSession,
        target_native_id: str | None,
        projects_by_key: dict[str, ProjectItem],
    ) -> tuple[str, bool]:
        raise NotImplementedError(f"{self.tool_name} does not support writing chat sessions")

    # --- Diagnostics & Repair ---

    def doctor(self) -> list[DoctorIssue]:
        """Validate native configs and session files for corruption or schema issues."""
        return []

    def repair(self, quarantine_dir: Path, dry_run: bool = False) -> RepairResult:
        """Repair or quarantine corrupted files detected by `doctor`."""
        return RepairResult(tool=self.tool_name)
