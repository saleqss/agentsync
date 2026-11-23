"""Continuous auto-diagnostics engine and deferred self-healing (`repair --on-close`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import psutil
from pydantic import BaseModel, Field

from agentsync.accel import runtime_engine_label
from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig
from agentsync.engine.autostart import (
    SplitInstallReport,
    autostart_status,
    detect_split_installations,
    enable_autostart,
    inspect_legacy_autostart_artifacts,
    purge_legacy_autostart_artifacts,
)
from agentsync.engine.doctor import DoctorEngine
from agentsync.models.canonical import DoctorIssue, RepairResult
from agentsync.state.backup import BackupManager
from agentsync.utils.atomic_io import atomic_write_json
from agentsync.utils.notify import send_desktop_notification
from agentsync.utils.process_guard import assert_apps_closed

# Issue codes considered safe for automatic non-destructive self-healing when apps close
SAFE_AUTO_HEAL_CODES: frozenset[str] = frozenset(
    {
        "AUTOSTART_LEGACY_OR_BROKEN",
        "DAEMON_STALE_LOCK",
        "CODEX_BROKEN_ROLLOUT",
        "CODEX_SPINNING_THREAD",
        "CODEX_ORPHAN_WAL",
        "CODEX_BROKEN_SKILL_SYMLINK",
        "CODEX_DUPLICATE_AGENT_SKILL",
        "CODEX_DUPLICATE_PROJECT",
        "AGY_MISSING_HUB_SUMMARY_PB",
        "AGY_RAW_SUMMARY_STATUS_MISMATCH",
        "AGY_CONV_DB_USER_VERSION",
    }
)


class RepairQueueEntry(BaseModel):
    tool: str
    code: str
    path: str = ""
    message: str
    safe_auto_heal: bool = True
    queued_at_utc: str = ""


class RepairQueue(BaseModel):
    updated_at_utc: str = ""
    entries: list[RepairQueueEntry] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return len(self.entries) == 0


class DiagnosticsReport(BaseModel):
    checked_at_utc: str
    issues: list[DoctorIssue] = Field(default_factory=list)
    fixable_count: int = 0
    error_count: int = 0
    warning_count: int = 0
    runtime_engine: str = "Pure Python"
    autostart_enabled: bool = False
    autostart_healthy: bool = True
    daemon_running: bool = False
    backup_count: int = 0
    pending_repairs: int = 0
    split_install: SplitInstallReport | None = None

    @property
    def actionable_count(self) -> int:
        return self.error_count + self.warning_count


class DeferredRepairResult(BaseModel):
    executed: bool = False
    backup_id: str | None = None
    fixed_count: int = 0
    remaining_issues: int = 0
    results: list[RepairResult] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


def load_repair_queue(queue_path: Path) -> RepairQueue:
    if not queue_path.exists():
        return RepairQueue()
    try:
        return RepairQueue.model_validate_json(queue_path.read_text(encoding="utf-8"))
    except Exception:
        return RepairQueue()


def save_repair_queue(queue_path: Path, queue: RepairQueue) -> None:
    queue.updated_at_utc = datetime.now(UTC).isoformat()
    atomic_write_json(queue_path, queue.model_dump(), RepairQueue)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False


class DiagnosticsEngine:
    """Runs comprehensive read-only system health checks and manages deferred self-healing."""

    def __init__(
        self,
        config: AgentSyncConfig,
        adapters: list[ToolAdapter],
        *,
        config_path: Path | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.config_path = config_path
        self.doctor = DoctorEngine(config, adapters)
        self.backup_mgr = BackupManager(config)

    def run_all(
        self,
        *,
        verify_backups: bool = False,
        check_autostart: bool = True,
        check_split_install: bool = True,
        check_daemon: bool = True,
    ) -> DiagnosticsReport:
        issues: list[DoctorIssue] = []

        # 1. Core adapter + SQLite + rollout + config diagnostics
        issues.extend(self.doctor.diagnose())

        # 2. Autostart task integrity & legacy script cleanup check
        auto_enabled = False
        auto_healthy = True
        if check_autostart:
            ast = autostart_status(self.config, config_path=self.config_path)
            auto_enabled = ast.enabled
            auto_healthy = ast.healthy
            for msg in ast.issues:
                issues.append(
                    DoctorIssue(
                        tool="agentsync",
                        severity="error",
                        code="AUTOSTART_LEGACY_OR_BROKEN",
                        path=ast.artifact_path,
                        message=msg,
                        fixable=True,
                    )
                )
            if ast.enabled and ast.healthy:
                # Still check for stray legacy launcher files even if task is already native
                for msg in inspect_legacy_autostart_artifacts(self.config):
                    issues.append(
                        DoctorIssue(
                            tool="agentsync",
                            severity="warning",
                            code="AUTOSTART_LEGACY_OR_BROKEN",
                            path=ast.artifact_path,
                            message=msg,
                            fixable=True,
                        )
                    )

        # 3. Split-installation version drift guard
        split_rep: SplitInstallReport | None = None
        if check_split_install:
            split_rep = detect_split_installations(self.config)
            if split_rep.has_version_drift:
                issues.append(
                    DoctorIssue(
                        tool="agentsync",
                        severity="warning",
                        code="SPLIT_INSTALL_VERSION_DRIFT",
                        path=split_rep.current_executable,
                        message=(
                            f"{split_rep.summary} Run 'agentsync update' to unify all installations."
                        ),
                        fixable=False,
                    )
                )

        # 4. Daemon lockfile & recent error log health
        daemon_running = False
        if check_daemon:
            # First check non-locked daemon_state.json for an active PID (always readable on Windows)
            if self.config.daemon_state_path.exists():
                try:
                    st_raw = json.loads(self.config.daemon_state_path.read_text(encoding="utf-8"))
                    st_pid = int(st_raw.get("pid") or 0)
                    if st_pid > 0 and _is_pid_alive(st_pid):
                        daemon_running = True
                except Exception:
                    pass

            if self.config.daemon_lock_path.exists() and not daemon_running:
                try:
                    raw = json.loads(self.config.daemon_lock_path.read_text(encoding="utf-8"))
                    pid = int(raw.get("pid") or 0)
                    if _is_pid_alive(pid):
                        daemon_running = True
                    else:
                        issues.append(
                            DoctorIssue(
                                tool="agentsync",
                                severity="warning",
                                code="DAEMON_STALE_LOCK",
                                path=str(self.config.daemon_lock_path),
                                message=f"Stale daemon lockfile found for dead PID {pid}.",
                                fixable=True,
                            )
                        )
                except PermissionError:
                    # Windows msvcrt.locking enforces a mandatory byte-range lock while a live daemon holds the file
                    daemon_running = True
                except Exception:
                    issues.append(
                        DoctorIssue(
                            tool="agentsync",
                            severity="warning",
                            code="DAEMON_STALE_LOCK",
                            path=str(self.config.daemon_lock_path),
                            message="Unreadable daemon lockfile detected.",
                            fixable=True,
                        )
                    )
            if self.config.daemon_log_path.exists():
                try:
                    tail = self.config.daemon_log_path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()[-40:]
                    recent_errs = [ln for ln in tail if "[ERROR]" in ln]
                    if recent_errs:
                        issues.append(
                            DoctorIssue(
                                tool="agentsync",
                                severity="warning",
                                code="DAEMON_RECENT_ERROR",
                                path=str(self.config.daemon_log_path),
                                message=f"Daemon log recorded {len(recent_errs)} recent error(s): {recent_errs[-1][:140]}",
                                fixable=False,
                            )
                        )
                except Exception:
                    pass

        # 5. Backup freshness & optional SHA-256 + SQLite integrity verification
        backups = self.backup_mgr.list_backups()
        if verify_backups and backups:
            for rep in self.backup_mgr.verify_all_backups():
                if not rep.ok:
                    issues.append(
                        DoctorIssue(
                            tool="agentsync",
                            severity="error",
                            code="BACKUP_INTEGRITY_FAILED",
                            path=str(self.config.backups_dir / rep.backup_id),
                            message=f"Backup '{rep.backup_id}' failed verification: {'; '.join(rep.errors[:2])}",
                            fixable=False,
                        )
                    )

        queue = load_repair_queue(self.config.repair_queue_path)
        return DiagnosticsReport(
            checked_at_utc=datetime.now(UTC).isoformat(),
            issues=issues,
            fixable_count=sum(1 for i in issues if i.fixable),
            error_count=sum(1 for i in issues if i.severity == "error"),
            warning_count=sum(1 for i in issues if i.severity == "warning"),
            runtime_engine=runtime_engine_label(self.config.runtime_engine),
            autostart_enabled=auto_enabled,
            autostart_healthy=auto_healthy,
            daemon_running=daemon_running,
            backup_count=len(backups),
            pending_repairs=len(queue.entries),
            split_install=split_rep,
        )

    def queue_repairs_on_close(
        self,
        issues: list[DoctorIssue] | None = None,
        *,
        safe_only: bool = False,
    ) -> RepairQueue:
        """Persist fixable issues to repair_queue.json for automatic execution once apps close."""
        if issues is None:
            report = self.run_all(verify_backups=False)
            issues = report.issues

        existing = load_repair_queue(self.config.repair_queue_path)
        by_key: dict[tuple[str, str, str], RepairQueueEntry] = {
            (e.tool, e.code, e.path): e for e in existing.entries
        }
        now_iso = datetime.now(UTC).isoformat()

        for iss in issues:
            if not iss.fixable:
                continue
            is_safe = iss.code in SAFE_AUTO_HEAL_CODES
            if safe_only and not is_safe:
                continue
            key = (iss.tool, iss.code, iss.path)
            if key not in by_key:
                by_key[key] = RepairQueueEntry(
                    tool=iss.tool,
                    code=iss.code,
                    path=iss.path,
                    message=iss.message,
                    safe_auto_heal=is_safe,
                    queued_at_utc=now_iso,
                )

        updated = RepairQueue(updated_at_utc=now_iso, entries=list(by_key.values()))
        save_repair_queue(self.config.repair_queue_path, updated)
        return updated

    def execute_queued_repairs(
        self,
        *,
        dry_run: bool = False,
        force: bool = False,
        check_processes: bool = True,
        safe_only: bool = False,
    ) -> DeferredRepairResult:
        """Execute queued self-healing repairs once locking applications have exited."""
        queue = load_repair_queue(self.config.repair_queue_path)
        entries = [e for e in queue.entries if (not safe_only or e.safe_auto_heal)]
        if not entries:
            return DeferredRepairResult(executed=False, details=["Repair queue is empty."])

        details: list[str] = []
        fixed = 0

        # 1. Handle agentsync-internal repairs (never blocked by AI desktop app locks)
        internal_codes = {e.code for e in entries if e.tool == "agentsync"}
        if "AUTOSTART_LEGACY_OR_BROKEN" in internal_codes:
            purged = purge_legacy_autostart_artifacts(self.config, dry_run=dry_run)
            details.extend(purged)
            fixed += len(purged)
            if self.config.autostart_enabled and not dry_run:
                try:
                    enable_autostart(self.config, config_path=self.config_path)
                    details.append("Re-registered native windowless autostart task.")
                    fixed += 1
                except Exception as exc:
                    details.append(f"Autostart re-registration note: {exc}")

        if "DAEMON_STALE_LOCK" in internal_codes and self.config.daemon_lock_path.exists():
            if dry_run:
                details.append(f"Would remove stale daemon lockfile: {self.config.daemon_lock_path}")
                fixed += 1
            else:
                self.config.daemon_lock_path.unlink(missing_ok=True)
                details.append("Removed stale daemon lockfile.")
                fixed += 1

        # 2. Handle tool-specific repairs (SQLite DBs, rollouts, Antigravity indexes, configs)
        tool_names = sorted({e.tool for e in entries if e.tool != "agentsync"})
        target_adapters = [a for a in self.adapters if a.tool_name in tool_names]
        backup_id: str | None = None
        results: list[RepairResult] = []

        if target_adapters:
            if not dry_run and check_processes:
                assert_apps_closed(force=force, check_apps=[a.tool_name for a in target_adapters])
            scoped_doc = DoctorEngine(self.config, target_adapters)
            results, backup_id = scoped_doc.repair(
                dry_run=dry_run,
                force=True,
                check_processes=False,
            )
            for rr in results:
                fixed += rr.fixed_count
                details.extend(rr.details)

        # 3. Verify post-repair health and clear resolved items from queue
        remaining = 0
        if not dry_run:
            post_issues = self.doctor.diagnose()
            remaining_fixable = {
                (i.tool, i.code, i.path) for i in post_issues if i.fixable
            }
            kept = [
                e
                for e in queue.entries
                if e.tool != "agentsync" and (e.tool, e.code, e.path) in remaining_fixable
            ]
            remaining = len(kept)
            save_repair_queue(self.config.repair_queue_path, RepairQueue(entries=kept))
            if fixed > 0:
                send_desktop_notification(
                    "AgentSync Auto-Heal Completed",
                    f"Repaired {fixed} issue(s) safely (backup={backup_id or 'none'}).",
                    enabled=self.config.notifications,
                )

        return DeferredRepairResult(
            executed=not dry_run,
            backup_id=backup_id,
            fixed_count=fixed,
            remaining_issues=remaining,
            results=results,
            details=details,
        )
