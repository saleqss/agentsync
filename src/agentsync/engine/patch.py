"""Auto-Patch and Manual Patch orchestrator with fast startup scan and strict checkbox isolation."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from agentsync.adapters.base import ToolAdapter
from agentsync.adapters.registry import get_registry
from agentsync.config import AgentSyncConfig, PatchMode, SyncOptions, ToolConfig
from agentsync.discovery import DiscoveredTool, DiscoveryEnv, discover_tools
from agentsync.engine.doctor import DoctorEngine
from agentsync.engine.sync import SyncEngine
from agentsync.models.canonical import DoctorIssue, RepairResult, SyncAction, SyncPlan
from agentsync.state.backup import BackupManager
from agentsync.state.ledger import LedgerItem, LedgerStore
from agentsync.utils.events import ProgressBus
from agentsync.utils.process_guard import detect_running_apps

AppHealthState = Literal["up_to_date", "needs_patch", "locked", "detected", "not_found"]


class DetectedAppStatus(BaseModel):
    """Real-time scan card for one AI coding client (desktop, CLI, or IDE)."""

    tool_name: str
    display_name: str
    client_kind: Literal["desktop", "cli", "ide", "both"] = "both"
    installed: bool = False
    checked: bool = False
    root_path: str = ""
    executable: str | None = None
    running_pids: list[int] = Field(default_factory=list)
    issue_count: int = 0
    fixable_count: int = 0
    status: AppHealthState = "not_found"
    entity_counts: dict[str, int] = Field(default_factory=dict)
    supported_entities: list[str] = Field(default_factory=list)
    issues: list[DoctorIssue] = Field(default_factory=list)


class PatchRunResult(BaseModel):
    """Aggregated result of a Manual or Auto patch run across checked apps only."""

    mode: PatchMode
    dry_run: bool
    selected_tools: list[str] = Field(default_factory=list)
    skipped_locked_tools: list[str] = Field(default_factory=list)
    backup_id: str | None = None
    ledger_path: str | None = None
    repair_results: list[RepairResult] = Field(default_factory=list)
    sync_plan: SyncPlan
    warnings: list[str] = Field(default_factory=list)


class PatchEngine:
    """Fast auto-scan + strict checkbox-scoped Auto-Patch / Manual Patch engine."""

    def __init__(
        self,
        config: AgentSyncConfig,
        bus: ProgressBus | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.registry = get_registry()
        self.backup_mgr = BackupManager(config)
        self.ledger = LedgerStore(config)

    def _adapter_for(self, tool_name: str, disc: DiscoveredTool | None = None) -> ToolAdapter:
        tcfg = self.config.tools.get(tool_name)
        if tcfg is None:
            tcfg = ToolConfig(
                enabled=bool(disc and disc.installed),
                path=disc.root_path if disc else None,
                extra_paths=dict(disc.extra_paths) if disc else {},
                entities=list(disc.supported_entities) if disc else [],
            )
        return self.registry.create(tool_name, tcfg)

    def scan(
        self,
        denv: DiscoveryEnv | None = None,
        *,
        check_processes: bool = True,
    ) -> list[DetectedAppStatus]:
        """Non-blocking parallel scan of all supported desktop/IDE/CLI tools and their health."""
        discovered = discover_tools(denv, bus=self.bus)
        running = detect_running_apps() if check_processes else []
        running_by_app: dict[str, list[int]] = {}
        for p in running:
            running_by_app.setdefault(p.app, []).append(p.pid)

        def _inspect_one(item: DiscoveredTool) -> DetectedAppStatus:
            tcfg = self.config.tools.get(item.name)
            path_str = (tcfg.path if tcfg and tcfg.path else item.root_path) or ""
            installed = item.installed or (bool(tcfg and tcfg.path) and Path(tcfg.path).exists())
            checked = tcfg.enabled if tcfg is not None else installed
            if not installed:
                return DetectedAppStatus(
                    tool_name=item.name,
                    display_name=item.display_name,
                    client_kind=item.client_kind,
                    installed=False,
                    checked=False,
                    root_path=path_str,
                    executable=item.executable,
                    status="not_found",
                    supported_entities=list(item.supported_entities),
                )

            adapter = self._adapter_for(item.name, item)
            counts: dict[str, int] = {}
            issues: list[DoctorIssue] = []
            try:
                if adapter.capabilities.skills:
                    counts["skills"] = len(adapter.read_skills())
                if adapter.capabilities.mcp:
                    counts["mcp"] = len(adapter.read_mcp_servers())
                if adapter.capabilities.projects:
                    projs = adapter.read_projects()
                    counts["projects"] = len(projs)
                else:
                    projs = {}
                if adapter.capabilities.chats:
                    counts["chats"] = len(adapter.read_chats(projs))
                if adapter.root_path.exists():
                    issues = adapter.doctor()
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool=item.name,
                        severity="warning",
                        code="SCAN_ERROR",
                        path=path_str,
                        message=str(exc),
                    )
                )

            pids = running_by_app.get(item.name, [])
            fixable = sum(1 for i in issues if i.fixable)
            if pids:
                status: AppHealthState = "locked"
            elif issues:
                status = "needs_patch"
            elif counts:
                status = "up_to_date"
            else:
                status = "detected"

            return DetectedAppStatus(
                tool_name=item.name,
                display_name=item.display_name,
                client_kind=item.client_kind,
                installed=True,
                checked=checked,
                root_path=path_str,
                executable=item.executable,
                running_pids=pids,
                issue_count=len(issues),
                fixable_count=fixable,
                status=status,
                entity_counts=counts,
                supported_entities=list(item.supported_entities),
                issues=issues,
            )

        items = [discovered[k] for k in sorted(discovered)]
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(items)))) as pool:
            results = list(pool.map(_inspect_one, items))
        # Sort installed first, then alphabetical
        results.sort(key=lambda s: (not s.installed, s.display_name.lower()))
        return results

    def _build_selected_adapters(
        self,
        selected_tools: list[str],
        discovered: dict[str, DiscoveredTool] | None = None,
    ) -> list[ToolAdapter]:
        """Instantiate ONLY checked `[x]` adapters so unchecked apps are strictly untouched."""
        adapters: list[ToolAdapter] = []
        disc_map = discovered or {}
        for tname in sorted(set(selected_tools)):
            if tname not in self.registry.list_names():
                continue
            adapter = self._adapter_for(tname, disc_map.get(tname))
            adapters.append(adapter)
        return adapters

    def run_patch(
        self,
        *,
        selected_tools: list[str],
        mode: PatchMode = "manual",
        dry_run: bool = False,
        options: SyncOptions | None = None,
        check_processes: bool = True,
        discovered: dict[str, DiscoveredTool] | None = None,
    ) -> PatchRunResult:
        """Execute Auto-Patch or Manual Patch strictly scoped to `selected_tools`."""
        opts = (options or SyncOptions()).model_copy(
            update={"dry_run": dry_run, "targets": list(selected_tools)}
        )
        warnings: list[str] = []
        skipped_locked: list[str] = []

        if self.bus:
            self.bus.emit("patch", "progress.init", step_index=0, total_steps=4, percent=5.0)

        effective_tools = list(selected_tools)
        if not dry_run and check_processes and not opts.force:
            running = detect_running_apps(check_apps=effective_tools)
            locked_set = sorted({p.app for p in running})
            if locked_set:
                skipped_locked = locked_set
                effective_tools = [t for t in effective_tools if t not in locked_set]
                warnings.append(
                    f"Skipped running/locked app(s): {', '.join(locked_set)} "
                    "(close them or enable --force to patch)."
                )

        # Ensure config enables selected tools transiently for this run without mutating unchecked tools
        scoped_cfg = self.config.model_copy(deep=True)
        for tname in effective_tools:
            if tname in scoped_cfg.tools:
                scoped_cfg.tools[tname].enabled = True
        for tname in list(scoped_cfg.tools):
            if tname not in effective_tools:
                scoped_cfg.tools[tname].enabled = False

        adapters = self._build_selected_adapters(effective_tools, discovered=discovered)
        backup_id: str | None = None

        if not dry_run and adapters:
            if self.bus:
                self.bus.check_cancelled()
                self.bus.emit("patch", "progress.backup", step_index=1, total_steps=4, percent=25.0)
            manifest = self.backup_mgr.create_backup(adapters, reason=f"patch-{mode}")
            backup_id = manifest.backup_id

        # Phase 1: Repair fixable issues on checked apps only
        repair_results: list[RepairResult] = []
        if adapters:
            doc_engine = DoctorEngine(scoped_cfg, adapters)
            for idx, a in enumerate(adapters, start=1):
                if self.bus:
                    self.bus.check_cancelled()
                    self.bus.emit(
                        "patch",
                        "progress.repairing",
                        step_index=2,
                        total_steps=4,
                        percent=35.0 + (idx / max(1, len(adapters))) * 15.0,
                        item_name=a.tool_name,
                        tool=a.display_name,
                    )
            rep_list, _ = doc_engine.repair(dry_run=dry_run, force=True, check_processes=False)
            repair_results = rep_list

        # Phase 2: Bidirectional sync across checked apps only
        sync_opts = opts.model_copy(update={"targets": [a.tool_name for a in adapters]})
        sync_engine = SyncEngine(scoped_cfg, adapters, options=sync_opts, bus=self.bus)
        plan = sync_engine.run(
            dry_run=dry_run,
            force=True,
            check_processes=False,
            create_backup=False,
        )
        plan.mode = "patch"
        plan.backup_id = backup_id or plan.backup_id

        # Surface repair items inside the unified SyncPlan diff table as well
        for rr in repair_results:
            for detail in rr.details:
                plan.add(
                    SyncAction(
                        category="chats",
                        action="REPAIR",
                        target_tool=rr.tool,
                        entity_id=rr.tool,
                        summary=detail,
                        risk_level="medium",
                    )
                )

        plan.warnings.extend(warnings)
        ledger_path = plan.ledger_path
        if not dry_run and (plan.mutating_actions or repair_results) and not ledger_path:
            run_id = str(uuid.uuid4())
            items = [
                LedgerItem(
                    category=a.category,
                    target_tool=a.target_tool,
                    entity_id=a.entity_id,
                    action=a.action,
                    risk_level=a.risk_level,
                    backup_path=str(self.config.backups_dir / backup_id) if backup_id else "",
                    summary=a.summary,
                )
                for a in plan.mutating_actions
            ]
            _, lpath = self.ledger.write_apply_ledger(
                run_id=run_id,
                mode=f"patch:{mode}",
                tools=[a.tool_name for a in adapters],
                items=items,
                backup_id=backup_id,
            )
            ledger_path = str(lpath)
            plan.ledger_path = ledger_path

        if self.bus:
            self.bus.emit("patch", "progress.done", step_index=4, total_steps=4, percent=100.0, level="success")

        return PatchRunResult(
            mode=mode,
            dry_run=dry_run,
            selected_tools=[a.tool_name for a in adapters],
            skipped_locked_tools=skipped_locked,
            backup_id=backup_id,
            ledger_path=ledger_path,
            repair_results=repair_results,
            sync_plan=plan,
            warnings=warnings,
        )
