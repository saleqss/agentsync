"""Diagnostic health checker (`doctor`) and safe quarantine/repair runner (`repair`)."""

from __future__ import annotations

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig
from agentsync.models.canonical import DoctorIssue, RepairResult
from agentsync.state.backup import BackupManager
from agentsync.utils.atomic_io import sqlite_safe_connect
from agentsync.utils.process_guard import assert_apps_closed


class DoctorEngine:
    """Validates all enabled tools' configs/sessions and repairs corrupted artifacts safely."""

    def __init__(self, config: AgentSyncConfig, adapters: list[ToolAdapter]) -> None:
        self.config = config
        self.adapters = adapters
        self.backup_mgr = BackupManager(config)

    def diagnose(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []

        # Verify agentsync's own state DB if it exists
        if self.config.state_db_path.exists():
            try:
                conn = sqlite_safe_connect(self.config.state_db_path, readonly=True)
                res = conn.execute("PRAGMA quick_check").fetchone()
                conn.close()
                if res and res[0] != "ok":
                    issues.append(
                        DoctorIssue(
                            tool="agentsync",
                            severity="error",
                            code="STATE_DB_CORRUPT",
                            path=str(self.config.state_db_path),
                            message=f"sync_state.db failed quick_check: {res[0]}",
                        )
                    )
            except Exception as exc:
                issues.append(
                    DoctorIssue(
                        tool="agentsync",
                        severity="error",
                        code="STATE_DB_UNREADABLE",
                        path=str(self.config.state_db_path),
                        message=f"Could not open sync_state.db: {exc}",
                    )
                )

        for adapter in self.adapters:
            if not adapter.root_path.exists():
                issues.append(
                    DoctorIssue(
                        tool=adapter.tool_name,
                        severity="warning",
                        code="TOOL_PATH_MISSING",
                        path=str(adapter.root_path),
                        message=f"Configured path for '{adapter.tool_name}' does not exist.",
                    )
                )
                continue
            issues.extend(adapter.doctor())

        return issues

    def repair(
        self,
        dry_run: bool = False,
        force: bool = False,
        check_processes: bool = True,
    ) -> tuple[list[RepairResult], str | None]:
        if not dry_run and check_processes:
            assert_apps_closed(force=force, check_apps=[a.tool_name for a in self.adapters])

        backup_id: str | None = None
        if not dry_run:
            manifest = self.backup_mgr.create_backup(self.adapters, reason="repair")
            backup_id = manifest.backup_id

        results: list[RepairResult] = []
        for adapter in self.adapters:
            if not adapter.root_path.exists():
                continue
            res = adapter.repair(self.config.quarantine_dir, dry_run=dry_run)
            if res.fixed_count > 0 or res.details:
                results.append(res)
        return results, backup_id
