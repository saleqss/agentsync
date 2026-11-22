"""One-time clean bootstrap migration (`bootstrap --from <tool>`)."""

from __future__ import annotations

import uuid
from pathlib import Path

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig, SyncOptions
from agentsync.engine.sync import SyncEngine
from agentsync.models.canonical import McpServer, PluginItem, SyncAction, SyncPlan
from agentsync.state.backup import BackupManager
from agentsync.state.state_db import EntityStateRecord, StateStore, now_ms
from agentsync.utils.paths import to_native_path
from agentsync.utils.process_guard import assert_apps_closed


class BootstrapEngine:
    """Cleanly resets target tools' skills/MCP/plugins from a single source of truth, then merges projects/rules/chats."""

    def __init__(
        self,
        config: AgentSyncConfig,
        adapters: list[ToolAdapter],
        options: SyncOptions | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.adapters_by_name = {a.tool_name: a for a in adapters}
        self.options = options or SyncOptions(include_subagents=config.include_subagents)
        self.backup_mgr = BackupManager(config)
        self.sync_engine = SyncEngine(config, adapters, options=self.options)

    def run_from(
        self,
        source_tool: str,
        dry_run: bool | None = None,
        force: bool | None = None,
        check_processes: bool = True,
    ) -> SyncPlan:
        if source_tool not in self.adapters_by_name:
            enabled = ", ".join(sorted(self.adapters_by_name)) or "(none)"
            raise ValueError(f"Source tool '{source_tool}' is not enabled. Enabled tools: {enabled}")

        is_dry = self.options.dry_run if dry_run is None else dry_run
        is_force = self.options.force if force is None else force

        if not is_dry and check_processes:
            assert_apps_closed(force=is_force, check_apps=[a.tool_name for a in self.adapters])

        source = self.adapters_by_name[source_tool]
        targets = [a for a in self.adapters if a.tool_name != source_tool]

        plan = SyncPlan(mode="bootstrap", dry_run=is_dry)
        run_id = str(uuid.uuid4())
        started_ms = now_ms()

        src_skills = source.read_skills() if source.capabilities.skills else {}
        src_mcps = source.read_mcp_servers() if source.capabilities.mcp else {}
        src_plugins = source.read_plugins() if source.capabilities.plugins else {}

        # Phase 1: Plan wipe of targets (skills, MCP, plugins)
        for tgt in targets:
            tname = tgt.tool_name
            if tgt.capabilities.skills and self.config.is_entity_enabled(tname, "skills"):
                for sname in sorted(tgt.read_skills()):
                    plan.add(
                        SyncAction(
                            category="skills",
                            action="DELETE",
                            target_tool=tname,
                            entity_id=sname,
                            summary=f"Clear existing skill '{sname}' in {tname} before import from {source_tool}",
                        )
                    )
            if tgt.capabilities.mcp and self.config.is_entity_enabled(tname, "mcp"):
                for mname, msrv in sorted(tgt.read_mcp_servers().items()):
                    if not msrv.portable:
                        continue
                    plan.add(
                        SyncAction(
                            category="mcp",
                            action="DELETE",
                            target_tool=tname,
                            entity_id=mname,
                            summary=f"Clear existing MCP '{mname}' in {tname} before import from {source_tool}",
                            details=msrv.masked_summary(),
                        )
                    )
            if tgt.capabilities.can_write("plugins") and self.config.is_entity_enabled(tname, "plugins"):
                existing_plugins = set(tgt.read_plugins())
                # Include stale empty plugin directories in Antigravity
                if hasattr(tgt, "plugins_dir") and tgt.plugins_dir.exists():  # type: ignore[attr-defined]
                    for d in tgt.plugins_dir.iterdir():  # type: ignore[attr-defined]
                        if d.is_dir() and not d.name.startswith("."):
                            existing_plugins.add(d.name)
                for pname in sorted(existing_plugins):
                    plan.add(
                        SyncAction(
                            category="plugins",
                            action="DELETE",
                            target_tool=tname,
                            entity_id=pname,
                            summary=f"Clear existing plugin '{pname}' in {tname} before import from {source_tool}",
                        )
                    )

        # Phase 2: Plan migration from source_tool -> each target
        portable_mcps_by_target: dict[str, dict[str, McpServer]] = {}
        portable_plugins_by_target: dict[str, dict[str, PluginItem]] = {}

        for tgt in targets:
            tname = tgt.tool_name
            if tgt.capabilities.skills and self.config.is_entity_enabled(tname, "skills"):
                for sname, sitem in sorted(src_skills.items()):
                    note = " (+frontmatter)" if sitem.synthesized_frontmatter else ""
                    plan.add(
                        SyncAction(
                            category="skills",
                            action="CREATE",
                            target_tool=tname,
                            entity_id=sname,
                            summary=f"Import skill '{sname}' from {source_tool} -> {tname}{note}",
                        )
                    )

            if tgt.capabilities.mcp and self.config.is_entity_enabled(tname, "mcp"):
                tgt_mcps: dict[str, McpServer] = {}
                for mname, msrv in sorted(src_mcps.items()):
                    if not msrv.portable:
                        plan.add(
                            SyncAction(
                                category="mcp",
                                action="SKIP",
                                target_tool=tname,
                                entity_id=mname,
                                summary=f"Skip non-portable {source_tool} MCP '{mname}' ({msrv.skip_reason})",
                                details=msrv.masked_summary(),
                            )
                        )
                        continue
                    if tname == "antigravity" and msrv.transport == "sse":
                        plan.add(
                            SyncAction(
                                category="mcp",
                                action="SKIP",
                                target_tool=tname,
                                entity_id=mname,
                                summary=f"Skip legacy SSE MCP '{mname}' (unsupported in antigravity)",
                                details=msrv.masked_summary(),
                            )
                        )
                        continue
                    tgt_mcps[mname] = msrv
                    plan.add(
                        SyncAction(
                            category="mcp",
                            action="CREATE",
                            target_tool=tname,
                            entity_id=mname,
                            summary=f"Import MCP '{mname}' ({msrv.transport}) from {source_tool} -> {tname}",
                            details=msrv.masked_summary(),
                        )
                    )
                portable_mcps_by_target[tname] = tgt_mcps

            if tgt.capabilities.can_write("plugins") and self.config.is_entity_enabled(tname, "plugins"):
                tgt_plugins: dict[str, PluginItem] = {}
                for pkey, pl in sorted(src_plugins.items()):
                    if not pl.portable:
                        plan.add(
                            SyncAction(
                                category="plugins",
                                action="SKIP",
                                target_tool=tname,
                                entity_id=pkey,
                                summary=f"Skip non-portable {source_tool} plugin '{pkey}' ({pl.skip_reason})",
                            )
                        )
                        continue
                    tgt_plugins[pl.name] = pl
                    plan.add(
                        SyncAction(
                            category="plugins",
                            action="CREATE",
                            target_tool=tname,
                            entity_id=pl.name,
                            summary=(
                                f"Convert plugin '{pl.name}' (skills={len(pl.skills)}, "
                                f"mcp={len(pl.mcp_servers)}) from {source_tool} -> {tname}"
                            ),
                        )
                    )
                portable_plugins_by_target[tname] = tgt_plugins

        if is_dry:
            state = StateStore(self.config.state_db_path, readonly=True)
            try:
                unified_projects = self.sync_engine._sync_projects(state, plan, dry_run=True)
                self.sync_engine._sync_instructions(state, plan, unified_projects, run_id, dry_run=True)
                self.sync_engine._sync_chats(state, plan, unified_projects, run_id, dry_run=True)
            finally:
                state.close()
            return plan

        # Phase 3: Full versioned backup before any mutation
        all_proj_dirs = [
            Path(to_native_path(p.display_path))
            for a in self.adapters
            if a.capabilities.projects
            for p in a.read_projects().values()
        ]
        manifest = self.backup_mgr.create_backup(
            self.adapters,
            reason=f"bootstrap-from-{source_tool}",
            extra_project_paths=all_proj_dirs,
        )
        plan.backup_id = manifest.backup_id

        # Wipe target skills/MCP/plugins cleanly
        for tgt in targets:
            tgt.clear_managed_for_bootstrap()

        state = StateStore(self.config.state_db_path, readonly=False)
        try:
            for cat in ("skills", "mcp", "plugins"):
                state.clear_category(cat)

            # Record source baseline & write into targets
            for sname, sitem in sorted(src_skills.items()):
                state.upsert_entity_state(
                    EntityStateRecord(
                        category="skills",
                        canonical_key=sname,
                        tool_name=source_tool,
                        native_id=sname,
                        content_hash=sitem.content_hash,
                        last_synced_hash=sitem.content_hash,
                        updated_at_ms=sitem.updated_at_ms,
                    )
                )
            for mname, msrv in sorted(src_mcps.items()):
                if msrv.portable:
                    state.upsert_entity_state(
                        EntityStateRecord(
                            category="mcp",
                            canonical_key=mname,
                            tool_name=source_tool,
                            native_id=mname,
                            content_hash=msrv.content_hash,
                            last_synced_hash=msrv.content_hash,
                            updated_at_ms=msrv.updated_at_ms,
                        )
                    )
            for pl in src_plugins.values():
                if pl.portable:
                    state.upsert_entity_state(
                        EntityStateRecord(
                            category="plugins",
                            canonical_key=pl.name,
                            tool_name=source_tool,
                            native_id=pl.name,
                            content_hash=pl.content_hash,
                            last_synced_hash=pl.content_hash,
                            updated_at_ms=pl.updated_at_ms,
                        )
                    )

            for tgt in targets:
                tname = tgt.tool_name
                if tgt.capabilities.skills and self.config.is_entity_enabled(tname, "skills"):
                    for sname, sitem in sorted(src_skills.items()):
                        tgt.write_skill(sitem)
                        state.upsert_entity_state(
                            EntityStateRecord(
                                category="skills",
                                canonical_key=sname,
                                tool_name=tname,
                                native_id=sname,
                                content_hash=sitem.content_hash,
                                last_synced_hash=sitem.content_hash,
                                updated_at_ms=now_ms(),
                            )
                        )
                if tname in portable_mcps_by_target:
                    _, warns = tgt.write_mcp_servers(portable_mcps_by_target[tname])
                    plan.warnings.extend(warns)
                    for mname, msrv in sorted(portable_mcps_by_target[tname].items()):
                        state.upsert_entity_state(
                            EntityStateRecord(
                                category="mcp",
                                canonical_key=mname,
                                tool_name=tname,
                                native_id=mname,
                                content_hash=msrv.content_hash,
                                last_synced_hash=msrv.content_hash,
                                updated_at_ms=now_ms(),
                            )
                        )
                if tname in portable_plugins_by_target:
                    for pname, pl in sorted(portable_plugins_by_target[tname].items()):
                        _, warns = tgt.write_plugin(pl)
                        plan.warnings.extend(warns)
                        re_pl = tgt.read_plugins().get(pname)
                        state.upsert_entity_state(
                            EntityStateRecord(
                                category="plugins",
                                canonical_key=pname,
                                tool_name=tname,
                                native_id=pname,
                                content_hash=re_pl.content_hash if re_pl else pl.content_hash,
                                last_synced_hash=pl.content_hash,
                                updated_at_ms=now_ms(),
                            )
                        )

            # Phase 4: Merge projects, instructions, and chats across all enabled tools
            unified_projects = self.sync_engine._sync_projects(state, plan, dry_run=False)
            self.sync_engine._sync_instructions(state, plan, unified_projects, run_id, dry_run=False)
            self.sync_engine._sync_chats(state, plan, unified_projects, run_id, dry_run=False)

            state.record_run(
                run_id=run_id,
                mode=f"bootstrap:{source_tool}",
                dry_run=False,
                status="completed",
                backup_id=plan.backup_id,
                started_at_ms=started_ms,
                summary={
                    "mutating_actions": len(plan.mutating_actions),
                    "skipped": len(plan.skipped_actions),
                    "conflicts": len(plan.conflict_actions),
                },
            )
        finally:
            state.close()

        return plan
