"""Live Context & Session Handoff engine with evidence-based DB verification and content-hash dedupe."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig
from agentsync.models.canonical import ChatMessage, ChatSession
from agentsync.state.backup import BackupManager
from agentsync.state.ledger import HandoffLedgerRecord, LedgerStore
from agentsync.utils.env_adapt import resolve_vendored_binary
from agentsync.utils.events import ProgressBus
from agentsync.utils.paths import to_native_path
from agentsync.utils.process_guard import assert_apps_closed
from agentsync.utils.terminology import translate_host_tokens

LaunchMode = Literal["none", "auto", "app", "terminal"]


class HandoffPreview(BaseModel):
    source_tool: str
    target_tool: str
    session_id: str
    title: str
    project_path: str | None = None
    message_count: int
    active_skills_count: int
    has_project_instructions: bool
    reusable_target_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


class HandoffResult(BaseModel):
    source_tool: str
    target_tool: str
    source_session_id: str
    target_session_id: str
    reused_existing: bool = False
    verified_on_disk: bool = False
    verified_message_count: int = 0
    synced_skills: int = 0
    synced_instructions: bool = False
    backup_id: str | None = None
    launched_command: str | None = None
    warnings: list[str] = Field(default_factory=list)


class HandoffEngine:
    """Transfers live conversations + project context + rules + skills across AI agents with DB proof."""

    def __init__(
        self,
        config: AgentSyncConfig,
        adapters_by_name: dict[str, ToolAdapter],
        bus: ProgressBus | None = None,
    ) -> None:
        self.config = config
        self.adapters_by_name = adapters_by_name
        self.ledger = LedgerStore(config)
        self.backup_mgr = BackupManager(config)
        self.bus = bus

    def list_sessions(self, tool_name: str, include_subagents: bool = False) -> list[ChatSession]:
        adapter = self.adapters_by_name.get(tool_name)
        if not adapter or not adapter.capabilities.chats:
            return []
        projects = adapter.read_projects() if adapter.capabilities.projects else {}
        chats = adapter.read_chats(projects, include_subagents=include_subagents)
        chats.sort(key=lambda c: c.updated_at_ms, reverse=True)
        return chats

    def _resolve_session(
        self, source_adapter: ToolAdapter, session_id: str | None
    ) -> tuple[ChatSession, dict[str, object]]:
        projects = source_adapter.read_projects() if source_adapter.capabilities.projects else {}
        chats = source_adapter.read_chats(projects, include_subagents=False)
        chats.sort(key=lambda c: c.updated_at_ms, reverse=True)
        if not chats:
            raise ValueError(f"No chat sessions found in '{source_adapter.tool_name}'.")
        if session_id:
            for c in chats:
                if c.native_id == session_id or c.native_id.startswith(session_id):
                    return c, projects
            raise ValueError(f"Session '{session_id}' not found in '{source_adapter.tool_name}'.")
        return chats[0], projects

    def preview(
        self,
        source_tool: str,
        target_tool: str,
        session_id: str | None = None,
    ) -> HandoffPreview:
        src = self.adapters_by_name.get(source_tool)
        tgt = self.adapters_by_name.get(target_tool)
        if not src:
            raise ValueError(f"Source tool '{source_tool}' is not enabled or configured.")
        if not tgt:
            raise ValueError(f"Target tool '{target_tool}' is not enabled or configured.")
        if not src.capabilities.chats:
            raise ValueError(f"Source tool '{source_tool}' does not support reading chats.")
        if not tgt.capabilities.can_write("chats"):
            raise ValueError(
                f"Target tool '{target_tool}' does not support writing chats safely (read-only or unsupported)."
            )

        chat, _ = self._resolve_session(src, session_id)
        existing = self.ledger.find_existing_handoff(
            source_tool, chat.native_id, target_tool, chat.content_hash
        )
        skills = src.read_skills() if src.capabilities.skills else {}
        has_proj_rules = False
        if chat.project_path:
            pdir = Path(to_native_path(chat.project_path))
            has_proj_rules = bool(src.read_project_instructions(pdir))

        return HandoffPreview(
            source_tool=source_tool,
            target_tool=target_tool,
            session_id=chat.native_id,
            title=chat.title or chat.native_id,
            project_path=chat.project_path,
            message_count=len(chat.messages),
            active_skills_count=len(skills),
            has_project_instructions=has_proj_rules,
            reusable_target_id=existing.target_session_id if existing else None,
        )

    def execute(
        self,
        source_tool: str,
        target_tool: str,
        *,
        session_id: str | None = None,
        open_mode: LaunchMode = "none",
        dry_run: bool = False,
        force: bool = False,
        check_processes: bool = True,
        sync_context: bool = True,
    ) -> HandoffResult:
        src = self.adapters_by_name.get(source_tool)
        tgt = self.adapters_by_name.get(target_tool)
        if not src or not tgt:
            raise ValueError(f"Both '{source_tool}' and '{target_tool}' must be available.")
        if not tgt.capabilities.can_write("chats"):
            raise ValueError(f"Target tool '{target_tool}' does not support chat writes.")

        if not dry_run and check_processes:
            assert_apps_closed(force=force, check_apps=[target_tool])

        chat, src_projects = self._resolve_session(src, session_id)
        warnings: list[str] = []

        # 1. Content-hash dedupe via import ledger (reuse unchanged thread cleanly)
        existing = self.ledger.find_existing_handoff(
            source_tool, chat.native_id, target_tool, chat.content_hash
        )
        if existing is not None:
            tgt_projs = tgt.read_projects() if tgt.capabilities.projects else {}
            tgt_chats = {c.native_id: c for c in tgt.read_chats(tgt_projs, include_subagents=True)}
            if existing.target_session_id in tgt_chats:
                verified_chat = tgt_chats[existing.target_session_id]
                launched = (
                    self._launch_target(target_tool, existing.target_session_id, chat.project_path, open_mode)
                    if not dry_run and open_mode != "none"
                    else None
                )
                return HandoffResult(
                    source_tool=source_tool,
                    target_tool=target_tool,
                    source_session_id=chat.native_id,
                    target_session_id=existing.target_session_id,
                    reused_existing=True,
                    verified_on_disk=True,
                    verified_message_count=len(verified_chat.messages),
                    launched_command=launched,
                )

        if dry_run:
            return HandoffResult(
                source_tool=source_tool,
                target_tool=target_tool,
                source_session_id=chat.native_id,
                target_session_id="(dry-run)",
                reused_existing=False,
                verified_on_disk=False,
                verified_message_count=len(chat.messages),
            )

        if self.bus:
            self.bus.emit(
                "handoff",
                "progress.backup",
                step_index=0,
                total_steps=3,
                percent=15.0,
                target=target_tool,
            )

        # Always create a verified SHA-256 pre-write snapshot of the target before mutating it
        backup_manifest = self.backup_mgr.create_backup([tgt], reason=f"handoff-{source_tool}-to-{target_tool}")

        if self.bus:
            self.bus.emit(
                "handoff",
                "progress.handoff_write",
                step_index=1,
                total_steps=3,
                percent=35.0,
                target=target_tool,
            )

        # 2. Ensure target knows the project, instructions, and skills before writing the thread
        tgt_projects = tgt.read_projects() if tgt.capabilities.projects else {}
        if chat.project_key and chat.project_key in src_projects and chat.project_key not in tgt_projects:
            tgt_projects[chat.project_key] = src_projects[chat.project_key]
            _, tgt_projects = tgt.write_projects(tgt_projects)

        synced_skills = 0
        synced_instructions = False
        if sync_context:
            if src.capabilities.skills and tgt.capabilities.skills:
                src_skills = src.read_skills()
                tgt_skills = tgt.read_skills()
                for sname, sitem in src_skills.items():
                    if sname not in tgt_skills:
                        tgt.write_skill(sitem)
                        synced_skills += 1
            if chat.project_path:
                pdir = Path(to_native_path(chat.project_path))
                pdoc = src.read_project_instructions(pdir)
                if pdoc and pdir.exists():
                    translated = translate_host_tokens(
                        pdoc.raw_text, source_tool, target_tool, strict_vocab_guard=self.config.strict_vocab_guard
                    )
                    for w in translated.warnings:
                        warnings.append(w.message)
                    out_doc = pdoc.model_copy(update={"raw_text": translated.translated_text})
                    tgt.write_project_instructions(pdir, out_doc)
                    synced_instructions = True

        # 3. Write session to target native store with clean distilled handoff metadata
        handoff_messages = list(chat.messages)
        if not handoff_messages:
            handoff_messages = [
                ChatMessage(
                    role="user",
                    text=f"[Handoff from {source_tool}] {chat.title}",
                    timestamp_ms=chat.created_at_ms,
                )
            ]
        transfer_chat = chat.model_copy(update={"messages": handoff_messages}).compute_hashes()
        target_sid, _ = tgt.write_chat(transfer_chat, target_native_id=None, projects_by_key=tgt_projects)

        if self.bus:
            self.bus.emit(
                "handoff",
                "progress.handoff_verify",
                step_index=2,
                total_steps=3,
                percent=75.0,
                target=target_tool,
            )

        # 4. Evidence-based verification directly against target native DB/store on disk
        refreshed_projs = tgt.read_projects() if tgt.capabilities.projects else {}
        re_read = {c.native_id: c for c in tgt.read_chats(refreshed_projs, include_subagents=True)}
        verified_chat = re_read.get(target_sid)
        verified = verified_chat is not None and len(verified_chat.messages) >= len(handoff_messages)
        if not verified:
            raise RuntimeError(
                f"Handoff write to '{target_tool}' could not be verified on disk for thread '{target_sid}'."
            )

        # 5. Record in Handoff Import Ledger for future content-hash dedupe
        self.ledger.record_handoff(
            HandoffLedgerRecord(
                source_tool=source_tool,
                source_session_id=chat.native_id,
                target_tool=target_tool,
                target_session_id=target_sid,
                content_hash=chat.content_hash,
                project_path=chat.project_path,
                verified_on_disk=True,
                transferred_at_utc=datetime.now(UTC).isoformat(),
            )
        )

        launched = (
            self._launch_target(target_tool, target_sid, chat.project_path, open_mode)
            if open_mode != "none"
            else None
        )
        if self.bus:
            self.bus.emit("handoff", "progress.done", step_index=3, total_steps=3, percent=100.0, level="success")

        return HandoffResult(
            source_tool=source_tool,
            target_tool=target_tool,
            source_session_id=chat.native_id,
            target_session_id=target_sid,
            reused_existing=False,
            verified_on_disk=True,
            verified_message_count=len(verified_chat.messages) if verified_chat else 0,
            synced_skills=synced_skills,
            synced_instructions=synced_instructions,
            backup_id=backup_manifest.backup_id,
            launched_command=launched,
            warnings=warnings,
        )

    def _launch_target(
        self, tool_name: str, session_id: str, project_path: str | None, mode: LaunchMode
    ) -> str | None:
        exe = resolve_vendored_binary(tool_name)
        if not exe:
            return None
        cwd = to_native_path(project_path) if project_path and Path(to_native_path(project_path)).exists() else None
        cmd = [exe]
        if mode == "terminal":
            if tool_name == "codex":
                cmd = [exe, "resume", session_id]
            elif tool_name == "claude-code":
                cmd = [exe, "--resume", session_id]
            elif tool_name == "opencode":
                cmd = [exe, "--session", session_id]
        try:
            subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return " ".join(cmd)
        except Exception:
            return None
