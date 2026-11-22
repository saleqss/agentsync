"""Multi-tool idempotent bidirectional synchronization engine."""

from __future__ import annotations

import uuid
from pathlib import Path

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig, EntityType, SyncOptions
from agentsync.engine.merge import (
    merge_markdown_texts,
    project_content_hash,
    save_conflict_snapshot,
    unify_projects_multi,
)
from agentsync.models.canonical import (
    ChatSession,
    InstructionDoc,
    McpServer,
    PluginItem,
    ProjectItem,
    SkillItem,
    SyncAction,
    SyncPlan,
)
from agentsync.state.backup import BackupManager
from agentsync.state.state_db import (
    ChatCanonicalRecord,
    ChatLinkRecord,
    EntityStateRecord,
    ProjectStateRecord,
    StateStore,
    now_ms,
)
from agentsync.utils.paths import to_native_path
from agentsync.utils.process_guard import assert_apps_closed


class SyncEngine:
    """Orchestrates strictly idempotent bidirectional sync across all enabled ToolAdapters."""

    def __init__(
        self,
        config: AgentSyncConfig,
        adapters: list[ToolAdapter],
        options: SyncOptions | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.adapters_by_name = {a.tool_name: a for a in adapters}
        self.options = options or SyncOptions(
            include_subagents=config.include_subagents,
            debounce_seconds=config.debounce_seconds,
        )
        self.backup_mgr = BackupManager(config)

    def _active_for(self, entity: EntityType) -> list[ToolAdapter]:
        return [
            a
            for a in self.adapters
            if a.capabilities.supports(entity) and self.config.is_entity_enabled(a.tool_name, entity)
        ]

    def run(
        self,
        dry_run: bool | None = None,
        force: bool | None = None,
        check_processes: bool = True,
        create_backup: bool = False,
    ) -> SyncPlan:
        is_dry = self.options.dry_run if dry_run is None else dry_run
        is_force = self.options.force if force is None else force

        if not is_dry and check_processes:
            assert_apps_closed(force=is_force, check_apps=[a.tool_name for a in self.adapters])

        plan = SyncPlan(mode="sync", dry_run=is_dry)
        run_id = str(uuid.uuid4())
        started_ms = now_ms()

        state = StateStore(self.config.state_db_path, readonly=is_dry)
        try:
            # 1. Projects
            unified_projects = self._sync_projects(state, plan, dry_run=is_dry)

            if create_backup and not is_dry and not plan.is_empty:
                extra_proj_dirs = [
                    Path(to_native_path(p.display_path)) for p in unified_projects.values()
                ]
                manifest = self.backup_mgr.create_backup(
                    self.adapters, reason="sync", extra_project_paths=extra_proj_dirs
                )
                plan.backup_id = manifest.backup_id

            # 2. Instructions (global + project)
            self._sync_instructions(state, plan, unified_projects, run_id, dry_run=is_dry)

            # 3. Skills
            self._sync_skills(state, plan, run_id, dry_run=is_dry)

            # 4. MCP servers
            self._sync_mcp(state, plan, run_id, dry_run=is_dry)

            # 5. Plugins
            self._sync_plugins(state, plan, run_id, dry_run=is_dry)

            # 6. Chats / sessions
            self._sync_chats(state, plan, unified_projects, run_id, dry_run=is_dry)

            if not is_dry and not plan.is_empty:
                state.record_run(
                    run_id=run_id,
                    mode="sync",
                    dry_run=False,
                    status="completed",
                    backup_id=plan.backup_id,
                    started_at_ms=started_ms,
                    summary={
                        "mutating_actions": len(plan.mutating_actions),
                        "skipped": len(plan.skipped_actions),
                        "conflicts": len(plan.conflict_actions),
                        "tools": [a.tool_name for a in self.adapters],
                    },
                )
        finally:
            state.close()

        return plan

    # --- 1. Projects ---

    def _sync_projects(
        self, state: StateStore, plan: SyncPlan, dry_run: bool
    ) -> dict[str, ProjectItem]:
        active = self._active_for("projects")
        if not active:
            return {}

        projects_by_tool = {a.tool_name: a.read_projects() for a in active}
        state_projects = state.list_project_states()
        named_tools = {a.tool_name for a in active if a.capabilities.project_names}
        unified = unify_projects_multi(projects_by_tool, state_projects, named_tools=named_tools)

        tools_needing_write: set[str] = set()
        state_needs_write: set[str] = set()

        for key, item in sorted(unified.items()):
            cur_hash = project_content_hash(item)
            st_map = state_projects.get(key) or {}
            for a in active:
                tname = a.tool_name
                tp = projects_by_tool[tname].get(key)
                st = st_map.get(tname)
                if tp is None:
                    tools_needing_write.add(tname)
                    state_needs_write.add(key)
                    plan.add(
                        SyncAction(
                            category="projects",
                            action="CREATE",
                            target_tool=tname,
                            entity_id=item.display_path,
                            summary=f"Create workspace '{item.name}' in {tname}",
                            details={"canonical_key": key},
                        )
                    )
                elif tp.duplicate_count(tname) > 0:
                    tools_needing_write.add(tname)
                    state_needs_write.add(key)
                    plan.add(
                        SyncAction(
                            category="projects",
                            action="MERGE",
                            target_tool=tname,
                            entity_id=item.display_path,
                            summary=(
                                f"Collapse {tp.duplicate_count(tname) + 1} duplicate entries "
                                f"for '{item.name}' in {tname}"
                            ),
                            details={"canonical_key": key},
                        )
                    )
                elif a.capabilities.project_names and tp.name != item.name:
                    tools_needing_write.add(tname)
                    state_needs_write.add(key)
                    plan.add(
                        SyncAction(
                            category="projects",
                            action="UPDATE",
                            target_tool=tname,
                            entity_id=item.display_path,
                            summary=f"Rename workspace to '{item.name}' in {tname}",
                            details={"canonical_key": key},
                        )
                    )
                elif st is None or st.last_synced_hash != cur_hash:
                    state_needs_write.add(key)
                    if st is None:
                        plan.add(
                            SyncAction(
                                category="projects",
                                action="LINK",
                                target_tool=tname,
                                entity_id=item.display_path,
                                summary=f"Link existing workspace '{item.name}' in {tname}",
                                details={"canonical_key": key},
                            )
                        )

        if not dry_run:
            for a in active:
                if a.tool_name in tools_needing_write:
                    _, unified = a.write_projects(unified)
            for key in sorted(state_needs_write):
                item = unified[key]
                cur_hash = project_content_hash(item)
                for a in active:
                    tname = a.tool_name
                    state.upsert_project_state(
                        ProjectStateRecord(
                            canonical_key=key,
                            tool_name=tname,
                            native_id=item.primary_id(tname),
                            secondary_id=item.secondary_id(tname),
                            display_path=item.display_path,
                            name=item.name,
                            last_synced_hash=cur_hash,
                            updated_at_ms=item.updated_at_ms or now_ms(),
                        )
                    )

        return unified

    # --- 2. Instructions ---

    def _reconcile_instruction_group(
        self,
        state: StateStore,
        plan: SyncPlan,
        canonical_key: str,
        label: str,
        scope: str,
        docs_by_tool: dict[str, InstructionDoc | None],
        writers_by_tool: dict[str, object],
        run_id: str,
        dry_run: bool,
        project_key: str | None = None,
        project_path: str | None = None,
    ) -> None:
        present = {t: d for t, d in docs_by_tool.items() if d is not None}
        if not present:
            return

        stored = state.get_entity_states("instructions", canonical_key)
        all_tools = list(docs_by_tool.keys())

        # Fast-path strict idempotency check
        if len(present) == len(all_tools) and all(
            t in stored and present[t].content_hash == stored[t].content_hash for t in all_tools
        ):
            return

        unique_hashes = {d.content_hash for d in present.values()}

        # Case A: All present tools already share the exact same content hash
        if len(unique_hashes) == 1:
            canonical_doc = next(iter(present.values()))
            missing_tools = [t for t in all_tools if t not in present]
            for mt in missing_tools:
                plan.add(
                    SyncAction(
                        category="instructions",
                        action="CREATE",
                        target_tool=mt,
                        entity_id=label,
                        summary=f"Write {label} instructions to {mt}",
                    )
                )
                if not dry_run:
                    _, warns = writers_by_tool[mt](canonical_doc)  # type: ignore[operator]
                    plan.warnings.extend(warns)
            if not missing_tools:
                for t in present:
                    if t not in stored or stored[t].content_hash != canonical_doc.content_hash:
                        plan.add(
                            SyncAction(
                                category="instructions",
                                action="LINK",
                                target_tool=t,
                                entity_id=label,
                                summary=f"Link identical {label} instructions in {t}",
                            )
                        )
            if not dry_run:
                for t in all_tools:
                    state.upsert_entity_state(
                        EntityStateRecord(
                            category="instructions",
                            canonical_key=canonical_key,
                            tool_name=t,
                            content_hash=canonical_doc.content_hash,
                            last_synced_hash=canonical_doc.content_hash,
                            updated_at_ms=now_ms(),
                        )
                    )
            return

        # Case B: Exactly one tool changed since last sync -> propagate it cleanly
        changed_tools = [
            t for t, d in present.items() if (t not in stored or d.content_hash != stored[t].content_hash)
        ]
        if len(changed_tools) == 1 and len(stored) >= 1:
            winner_tool = changed_tools[0]
            winner_doc = present[winner_tool]
            for t in all_tools:
                if t == winner_tool:
                    continue
                if t in present and present[t].content_hash == winner_doc.content_hash:
                    continue
                act_kind = "CREATE" if t not in present else "UPDATE"
                plan.add(
                    SyncAction(
                        category="instructions",
                        action=act_kind,
                        target_tool=t,
                        entity_id=label,
                        summary=f"Propagate {label} instructions from {winner_tool} -> {t}",
                    )
                )
                if not dry_run:
                    _, warns = writers_by_tool[t](winner_doc)  # type: ignore[operator]
                    plan.warnings.extend(warns)
            if not dry_run:
                for t in all_tools:
                    state.upsert_entity_state(
                        EntityStateRecord(
                            category="instructions",
                            canonical_key=canonical_key,
                            tool_name=t,
                            content_hash=winner_doc.content_hash,
                            last_synced_hash=winner_doc.content_hash,
                            updated_at_ms=now_ms(),
                        )
                    )
            return

        # Case C: Multiple differing versions -> deduplicated Markdown section merge (latest updated_at wins conflicts)
        ordered = sorted(present.items(), key=lambda kv: kv[1].updated_at_ms, reverse=True)
        winner_tool, winner_doc = ordered[0]
        merged_text = winner_doc.raw_text
        all_conflict_headings: list[str] = []
        losers_with_conflicts: list[tuple[str, InstructionDoc, list[str]]] = []

        for other_tool, other_doc in ordered[1:]:
            merged_text, conf_h = merge_markdown_texts(merged_text, other_doc.raw_text, primary_wins=True)
            if conf_h:
                all_conflict_headings.extend(conf_h)
                losers_with_conflicts.append((other_tool, other_doc, conf_h))

        merged_doc = InstructionDoc.from_markdown(
            raw_markdown=merged_text,
            scope=scope,  # type: ignore[arg-type]
            source_tool=winner_tool,
            updated_at_ms=winner_doc.updated_at_ms,
            project_key=project_key,
            project_path=project_path,
        )
        act_kind = "CONFLICT" if all_conflict_headings else "MERGE"
        plan.add(
            SyncAction(
                category="instructions",
                action=act_kind,
                target_tool=",".join(all_tools),
                entity_id=label,
                summary=(
                    f"Merge {label} instructions (winner={winner_tool}, "
                    f"conflicting sections={len(all_conflict_headings)})"
                ),
                details={"winner": winner_tool, "conflict_headings": all_conflict_headings},
            )
        )
        if not dry_run:
            for loser_tool, loser_doc, conf_h in losers_with_conflicts:
                bpath = save_conflict_snapshot(
                    self.config.conflicts_dir, "instructions", label, loser_tool, loser_doc.raw_text
                )
                state.record_conflict(
                    run_id=run_id,
                    category="instructions",
                    entity_id=label,
                    winner_tool=winner_tool,
                    loser_tool=loser_tool,
                    backup_path=str(bpath),
                    details=f"Conflicting headings: {', '.join(conf_h)}",
                )
            for t in all_tools:
                if t not in present or present[t].content_hash != merged_doc.content_hash:
                    _, warns = writers_by_tool[t](merged_doc)  # type: ignore[operator]
                    plan.warnings.extend(warns)
                state.upsert_entity_state(
                    EntityStateRecord(
                        category="instructions",
                        canonical_key=canonical_key,
                        tool_name=t,
                        content_hash=merged_doc.content_hash,
                        last_synced_hash=merged_doc.content_hash,
                        updated_at_ms=now_ms(),
                    )
                )

    def _sync_instructions(
        self,
        state: StateStore,
        plan: SyncPlan,
        projects: dict[str, ProjectItem],
        run_id: str,
        dry_run: bool,
    ) -> None:
        active = self._active_for("instructions")
        if len(active) < 2:
            return

        # Global instructions (only tools that support global instruction files; e.g. DSH is project-only)
        global_tools = [a for a in active if a.tool_name != "deepseek-harness"]
        if len(global_tools) >= 2:
            self._reconcile_instruction_group(
                state=state,
                plan=plan,
                canonical_key="global",
                label="global",
                scope="global",
                docs_by_tool={a.tool_name: a.read_global_instructions() for a in global_tools},
                writers_by_tool={a.tool_name: a.write_global_instructions for a in global_tools},
                run_id=run_id,
                dry_run=dry_run,
            )

        # Project instructions (only for existing project dirs that have at least one rule file)
        for key, proj in sorted(projects.items()):
            pdir = Path(to_native_path(proj.display_path))
            if not pdir.exists() or not pdir.is_dir():
                continue
            docs = {a.tool_name: a.read_project_instructions(pdir) for a in active}
            if not any(docs.values()):
                continue
            self._reconcile_instruction_group(
                state=state,
                plan=plan,
                canonical_key=f"project:{key}",
                label=f"project:{proj.name}",
                scope="project",
                docs_by_tool=docs,
                writers_by_tool={
                    a.tool_name: (lambda doc, adapter=a, path=pdir: adapter.write_project_instructions(path, doc))
                    for a in active
                },
                run_id=run_id,
                dry_run=dry_run,
                project_key=key,
                project_path=proj.display_path,
            )

    # --- 3. Skills ---

    def _sync_skills(
        self, state: StateStore, plan: SyncPlan, run_id: str, dry_run: bool
    ) -> None:
        active = self._active_for("skills")
        if len(active) < 2:
            return

        skills_by_tool: dict[str, dict[str, SkillItem]] = {a.tool_name: a.read_skills() for a in active}
        all_names = sorted({name for smap in skills_by_tool.values() for name in smap})

        for name in all_names:
            present = {t: smap[name] for t, smap in skills_by_tool.items() if name in smap}
            stored = state.get_entity_states("skills", name)
            all_tools = [a.tool_name for a in active]

            if len(present) == len(all_tools) and all(
                t in stored and present[t].content_hash == stored[t].content_hash for t in all_tools
            ):
                continue

            unique_hashes = {s.content_hash for s in present.values()}
            if len(unique_hashes) == 1:
                winner = next(iter(present.values()))
                for a in active:
                    if a.tool_name not in present:
                        plan.add(
                            SyncAction(
                                category="skills",
                                action="CREATE",
                                target_tool=a.tool_name,
                                entity_id=name,
                                summary=f"Copy skill '{name}' from {winner.source_tool} -> {a.tool_name}",
                            )
                        )
                        if not dry_run:
                            a.write_skill(winner)
                    elif a.tool_name not in stored:
                        plan.add(
                            SyncAction(
                                category="skills",
                                action="LINK",
                                target_tool=a.tool_name,
                                entity_id=name,
                                summary=f"Link identical skill '{name}' in {a.tool_name}",
                            )
                        )
                if not dry_run:
                    for a in active:
                        state.upsert_entity_state(
                            EntityStateRecord(
                                category="skills",
                                canonical_key=name,
                                tool_name=a.tool_name,
                                native_id=name,
                                content_hash=winner.content_hash,
                                last_synced_hash=winner.content_hash,
                                updated_at_ms=now_ms(),
                            )
                        )
                continue

            changed_tools = [
                t for t, s in present.items() if (t not in stored or s.content_hash != stored[t].content_hash)
            ]
            if len(changed_tools) == 1 and len(stored) >= 1:
                winner_tool = changed_tools[0]
                winner = present[winner_tool]
                is_conflict = False
            else:
                winner_tool, winner = max(present.items(), key=lambda kv: kv[1].updated_at_ms)
                is_conflict = len(present) > 1

            for a in active:
                tname = a.tool_name
                if tname in present and present[tname].content_hash == winner.content_hash:
                    continue
                act_kind = "CREATE" if tname not in present else ("CONFLICT" if is_conflict else "UPDATE")
                plan.add(
                    SyncAction(
                        category="skills",
                        action=act_kind,
                        target_tool=tname,
                        entity_id=name,
                        summary=f"Update skill '{name}' in {tname} from {winner_tool}",
                    )
                )
                if not dry_run:
                    if is_conflict and tname in present:
                        loser = present[tname]
                        bpath = save_conflict_snapshot(
                            self.config.conflicts_dir,
                            "skills",
                            name,
                            tname,
                            {
                                "name": loser.name,
                                "content_hash": loser.content_hash,
                                "skill_md": loser.files.get("SKILL.md", b"").decode("utf-8", errors="replace"),
                            },
                        )
                        state.record_conflict(
                            run_id=run_id,
                            category="skills",
                            entity_id=name,
                            winner_tool=winner_tool,
                            loser_tool=tname,
                            backup_path=str(bpath),
                            details="Concurrent skill edit resolved by updated_at",
                        )
                    a.write_skill(winner)

            if not dry_run:
                for a in active:
                    state.upsert_entity_state(
                        EntityStateRecord(
                            category="skills",
                            canonical_key=name,
                            tool_name=a.tool_name,
                            native_id=name,
                            content_hash=winner.content_hash,
                            last_synced_hash=winner.content_hash,
                            updated_at_ms=now_ms(),
                        )
                    )

    # --- 4. MCP Servers ---

    def _sync_mcp(
        self, state: StateStore, plan: SyncPlan, run_id: str, dry_run: bool
    ) -> None:
        active = self._active_for("mcp")
        if len(active) < 2:
            return

        mcps_by_tool: dict[str, dict[str, McpServer]] = {a.tool_name: a.read_mcp_servers() for a in active}
        merged_by_tool: dict[str, dict[str, McpServer]] = {t: dict(m) for t, m in mcps_by_tool.items()}
        dirty_tools: set[str] = set()
        state_updates: list[EntityStateRecord] = []

        all_names = sorted({name for mmap in mcps_by_tool.values() for name in mmap})
        for name in all_names:
            # Skip non-portable internal runtime servers (e.g. Codex node_repl)
            non_portable = next(
                (mmap[name] for mmap in mcps_by_tool.values() if name in mmap and not mmap[name].portable),
                None,
            )
            if non_portable is not None:
                plan.add(
                    SyncAction(
                        category="mcp",
                        action="SKIP",
                        target_tool="all",
                        entity_id=name,
                        summary=f"Skip non-portable MCP '{name}' ({non_portable.skip_reason})",
                        details=non_portable.masked_summary(),
                    )
                )
                continue

            present = {t: mmap[name] for t, mmap in mcps_by_tool.items() if name in mmap}
            stored = state.get_entity_states("mcp", name)

            # Determine which tools can actually host this transport (Antigravity rejects legacy SSE)
            sample = next(iter(present.values()))
            eligible = [
                a for a in active if not (a.tool_name == "antigravity" and sample.transport == "sse")
            ]
            for a in active:
                if a.tool_name == "antigravity" and sample.transport == "sse" and a.tool_name not in present:
                    plan.add(
                        SyncAction(
                            category="mcp",
                            action="SKIP",
                            target_tool="antigravity",
                            entity_id=name,
                            summary=f"Skip legacy SSE MCP '{name}' for antigravity (unsupported transport)",
                            details=sample.masked_summary(),
                        )
                    )

            if not eligible:
                continue

            if all(
                a.tool_name in present
                and a.tool_name in stored
                and present[a.tool_name].content_hash == stored[a.tool_name].content_hash
                for a in eligible
            ):
                continue

            unique_hashes = {m.content_hash for m in present.values()}
            if len(unique_hashes) == 1:
                winner = next(iter(present.values()))
                winner_tool = winner.source_tool
                is_conflict = False
            else:
                changed = [
                    t for t, m in present.items() if (t not in stored or m.content_hash != stored[t].content_hash)
                ]
                if len(changed) == 1 and len(stored) >= 1:
                    winner_tool = changed[0]
                    winner = present[winner_tool]
                    is_conflict = False
                else:
                    winner_tool, winner = max(present.items(), key=lambda kv: kv[1].updated_at_ms)
                    is_conflict = len(present) > 1

            for a in eligible:
                tname = a.tool_name
                if tname in present and present[tname].content_hash == winner.content_hash:
                    if tname not in stored:
                        plan.add(
                            SyncAction(
                                category="mcp",
                                action="LINK",
                                target_tool=tname,
                                entity_id=name,
                                summary=f"Link identical MCP '{name}' in {tname}",
                                details=winner.masked_summary(),
                            )
                        )
                else:
                    act_kind = "CREATE" if tname not in present else ("CONFLICT" if is_conflict else "UPDATE")
                    plan.add(
                        SyncAction(
                            category="mcp",
                            action=act_kind,
                            target_tool=tname,
                            entity_id=name,
                            summary=f"Sync MCP '{name}' ({winner.transport}) -> {tname}",
                            details=winner.masked_summary(),
                        )
                    )
                    if is_conflict and tname in present and not dry_run:
                        bpath = save_conflict_snapshot(
                            self.config.conflicts_dir, "mcp", name, tname, present[tname].model_dump()
                        )
                        state.record_conflict(
                            run_id=run_id,
                            category="mcp",
                            entity_id=name,
                            winner_tool=winner_tool,
                            loser_tool=tname,
                            backup_path=str(bpath),
                            details="Concurrent MCP edit resolved by updated_at",
                        )
                    merged_by_tool[tname][name] = winner
                    dirty_tools.add(tname)

                state_updates.append(
                    EntityStateRecord(
                        category="mcp",
                        canonical_key=name,
                        tool_name=tname,
                        native_id=name,
                        content_hash=winner.content_hash,
                        last_synced_hash=winner.content_hash,
                        updated_at_ms=now_ms(),
                    )
                )

        if not dry_run:
            for a in active:
                if a.tool_name in dirty_tools:
                    _, warns = a.write_mcp_servers(merged_by_tool[a.tool_name])
                    plan.warnings.extend(warns)
            for upd in state_updates:
                state.upsert_entity_state(upd)

    # --- 5. Plugins ---

    def _sync_plugins(
        self, state: StateStore, plan: SyncPlan, run_id: str, dry_run: bool
    ) -> None:
        active = [a for a in self._active_for("plugins") if a.capabilities.can_write("plugins")]
        if len(active) < 2:
            return

        raw_by_tool = {a.tool_name: a.read_plugins() for a in active}
        portable_by_tool: dict[str, dict[str, PluginItem]] = {}

        for tname, pmap in raw_by_tool.items():
            portable: dict[str, PluginItem] = {}
            for pkey, pl in sorted(pmap.items()):
                if not pl.portable:
                    plan.add(
                        SyncAction(
                            category="plugins",
                            action="SKIP",
                            target_tool="all",
                            entity_id=pkey,
                            summary=f"Skip non-portable {tname} plugin '{pkey}' ({pl.skip_reason})",
                        )
                    )
                    continue
                portable[pl.name] = pl
            portable_by_tool[tname] = portable

        all_names = sorted({n for pmap in portable_by_tool.values() for n in pmap})
        for name in all_names:
            present = {t: pmap[name] for t, pmap in portable_by_tool.items() if name in pmap}
            stored = state.get_entity_states("plugins", name)
            all_tools = [a.tool_name for a in active]

            if len(present) == len(all_tools) and all(
                t in stored and present[t].content_hash == stored[t].content_hash for t in all_tools
            ):
                continue

            unique_hashes = {p.content_hash for p in present.values()}
            if len(unique_hashes) == 1:
                winner = next(iter(present.values()))
                winner_tool = winner.source_tool
                is_conflict = False
            else:
                changed = [
                    t for t, p in present.items() if (t not in stored or p.content_hash != stored[t].content_hash)
                ]
                if len(changed) == 1 and len(stored) >= 1:
                    winner_tool = changed[0]
                    winner = present[winner_tool]
                    is_conflict = False
                else:
                    winner_tool, winner = max(present.items(), key=lambda kv: kv[1].updated_at_ms)
                    is_conflict = len(present) > 1

            for a in active:
                tname = a.tool_name
                if tname in present and present[tname].content_hash == winner.content_hash:
                    if tname not in stored:
                        plan.add(
                            SyncAction(
                                category="plugins",
                                action="LINK",
                                target_tool=tname,
                                entity_id=name,
                                summary=f"Link identical plugin '{name}' in {tname}",
                            )
                        )
                        if not dry_run:
                            state.upsert_entity_state(
                                EntityStateRecord(
                                    category="plugins",
                                    canonical_key=name,
                                    tool_name=tname,
                                    native_id=name,
                                    content_hash=present[tname].content_hash,
                                    last_synced_hash=winner.content_hash,
                                    updated_at_ms=now_ms(),
                                )
                            )
                    continue

                act_kind = "CREATE" if tname not in present else ("CONFLICT" if is_conflict else "UPDATE")
                plan.add(
                    SyncAction(
                        category="plugins",
                        action=act_kind,
                        target_tool=tname,
                        entity_id=name,
                        summary=(
                            f"Convert plugin '{name}' (skills={len(winner.skills)}, "
                            f"mcp={len(winner.mcp_servers)}) -> {tname}"
                        ),
                    )
                )
                if not dry_run:
                    if is_conflict and tname in present:
                        bpath = save_conflict_snapshot(
                            self.config.conflicts_dir,
                            "plugins",
                            name,
                            tname,
                            present[tname].model_dump(exclude={"skills"}),
                        )
                        state.record_conflict(
                            run_id=run_id,
                            category="plugins",
                            entity_id=name,
                            winner_tool=winner_tool,
                            loser_tool=tname,
                            backup_path=str(bpath),
                            details="Concurrent plugin edit resolved by updated_at",
                        )
                    _, warns = a.write_plugin(winner)
                    plan.warnings.extend(warns)
                    re_read = a.read_plugins().get(name)
                    state.upsert_entity_state(
                        EntityStateRecord(
                            category="plugins",
                            canonical_key=name,
                            tool_name=tname,
                            native_id=name,
                            content_hash=re_read.content_hash if re_read else winner.content_hash,
                            last_synced_hash=winner.content_hash,
                            updated_at_ms=now_ms(),
                        )
                    )

            if not dry_run and winner_tool in present:
                state.upsert_entity_state(
                    EntityStateRecord(
                        category="plugins",
                        canonical_key=name,
                        tool_name=winner_tool,
                        native_id=name,
                        content_hash=present[winner_tool].content_hash,
                        last_synced_hash=winner.content_hash,
                        updated_at_ms=now_ms(),
                    )
                )

    # --- 6. Chats / Sessions ---

    def _sync_chats(
        self,
        state: StateStore,
        plan: SyncPlan,
        projects: dict[str, ProjectItem],
        run_id: str,
        dry_run: bool,
    ) -> None:
        active = self._active_for("chats")
        if len(active) < 2:
            return

        writable_tools = [a for a in active if a.capabilities.can_write("chats")]
        readonly_tools = [a for a in active if not a.capabilities.can_write("chats")]
        for ro in readonly_tools:
            plan.add(
                SyncAction(
                    category="chats",
                    action="SKIP",
                    target_tool=ro.tool_name,
                    entity_id="chat-write",
                    summary=f"Skip writing chats to {ro.tool_name} (read-only chat adapter)",
                )
            )

        chats_by_tool: dict[str, dict[str, ChatSession]] = {
            a.tool_name: {
                c.native_id: c
                for c in a.read_chats(projects, include_subagents=self.options.include_subagents)
            }
            for a in active
        }

        matched_ids: dict[str, set[str]] = {a.tool_name: set() for a in active}
        groups: list[tuple[ChatCanonicalRecord | None, dict[str, ChatSession]]] = []

        # Pass 1: Match existing canonical chats from sync_state.db
        for canon in state.list_chats():
            group_sessions: dict[str, ChatSession] = {}
            for tname, link in canon.links.items():
                if tname in chats_by_tool and link.native_chat_id in chats_by_tool[tname]:
                    group_sessions[tname] = chats_by_tool[tname][link.native_chat_id]
                    matched_ids[tname].add(link.native_chat_id)
            if group_sessions:
                groups.append((canon, group_sessions))

        # Pass 2: Match remaining unmapped chats across tools by deterministic SHA-256 fingerprint
        fp_buckets: dict[str, list[dict[str, ChatSession]]] = {}
        for a in active:
            tname = a.tool_name
            for nid, sess in chats_by_tool[tname].items():
                if nid in matched_ids[tname]:
                    continue
                matched_ids[tname].add(nid)
                placed = False
                for bucket in fp_buckets.setdefault(sess.fingerprint, []):
                    if tname not in bucket:
                        bucket[tname] = sess
                        placed = True
                        break
                if not placed:
                    fp_buckets[sess.fingerprint].append({tname: sess})

        for buckets in fp_buckets.values():
            for bucket in buckets:
                groups.append((None, bucket))

        # Pass 3: Reconcile each canonical chat group across all writable tools
        for canon, present in groups:
            # Check strict idempotency
            if (
                canon is not None
                and all(a.tool_name in present for a in writable_tools)
                and all(
                    t in canon.links and s.content_hash == canon.links[t].content_hash
                    for t, s in present.items()
                )
            ):
                continue

            unique_hashes = {s.content_hash for s in present.values()}
            if len(unique_hashes) == 1:
                winner_tool, winner = next(iter(present.items()))
                is_conflict = False
            else:
                changed = [
                    t
                    for t, s in present.items()
                    if (
                        canon is None
                        or t not in canon.links
                        or s.content_hash != canon.links[t].content_hash
                    )
                ]
                if len(changed) == 1 and canon is not None and len(canon.links) >= 1:
                    winner_tool = changed[0]
                    winner = present[winner_tool]
                    is_conflict = False
                else:
                    winner_tool, winner = max(present.items(), key=lambda kv: kv[1].updated_at_ms)
                    is_conflict = len(present) > 1

            canonical_id = canon.canonical_id if canon else str(uuid.uuid4())
            updated_links: dict[str, ChatLinkRecord] = dict(canon.links) if canon else {}

            # Ensure present matching tools are linked
            for tname, sess in present.items():
                if sess.content_hash == winner.content_hash:
                    if canon is None or tname not in canon.links:
                        if len(present) > 1:
                            plan.add(
                                SyncAction(
                                    category="chats",
                                    action="LINK",
                                    target_tool=tname,
                                    entity_id=sess.native_id,
                                    summary=f"Link duplicate chat '{sess.title[:50]}' by fingerprint in {tname}",
                                    details={"fingerprint": sess.fingerprint[:12]},
                                )
                            )
                    updated_links[tname] = ChatLinkRecord(
                        canonical_id=canonical_id,
                        tool_name=tname,
                        native_chat_id=sess.native_id,
                        content_hash=sess.content_hash,
                        updated_at_ms=sess.updated_at_ms,
                    )

            # Propagate winner to every writable tool missing it or having stale/conflicting content
            for a in writable_tools:
                tname = a.tool_name
                if tname in present and present[tname].content_hash == winner.content_hash:
                    continue
                existing_sess = present.get(tname)
                act_kind = (
                    "CREATE"
                    if existing_sess is None
                    else ("CONFLICT" if is_conflict else "UPDATE")
                )
                plan.add(
                    SyncAction(
                        category="chats",
                        action=act_kind,
                        target_tool=tname,
                        entity_id=existing_sess.native_id if existing_sess else winner.native_id,
                        summary=(
                            f"{'Resolve chat conflict' if is_conflict else 'Sync chat'} "
                            f"'{winner.title[:50]}' ({len(winner.messages)} msgs) -> {tname} "
                            f"(source={winner_tool})"
                        ),
                        details={
                            "project_key": winner.project_key,
                            "fingerprint": winner.fingerprint[:12],
                        },
                    )
                )
                if not dry_run:
                    if is_conflict and existing_sess is not None:
                        bpath = save_conflict_snapshot(
                            self.config.conflicts_dir,
                            "chats",
                            f"{canonical_id[:8]}_{tname}",
                            tname,
                            existing_sess.model_dump(),
                        )
                        state.record_conflict(
                            run_id=run_id,
                            category="chats",
                            entity_id=canonical_id,
                            winner_tool=winner_tool,
                            loser_tool=tname,
                            backup_path=str(bpath),
                            details=(
                                f"LWW winner={winner_tool} ({winner.updated_at_ms} >= "
                                f"{existing_sess.updated_at_ms})"
                            ),
                        )
                    target_nid = (
                        existing_sess.native_id
                        if existing_sess
                        else (updated_links[tname].native_chat_id if tname in updated_links else None)
                    )
                    written_id, _ = a.write_chat(winner, target_native_id=target_nid, projects_by_key=projects)
                    updated_links[tname] = ChatLinkRecord(
                        canonical_id=canonical_id,
                        tool_name=tname,
                        native_chat_id=written_id,
                        content_hash=winner.content_hash,
                        updated_at_ms=winner.updated_at_ms,
                    )

            if not dry_run:
                state.upsert_chat(
                    ChatCanonicalRecord(
                        canonical_id=canonical_id,
                        fingerprint=winner.fingerprint,
                        project_key=winner.project_key,
                        title=winner.title,
                        last_synced_hash=winner.content_hash,
                        last_synced_at_ms=now_ms(),
                        links=updated_links,
                    )
                )
