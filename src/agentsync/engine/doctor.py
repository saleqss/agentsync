"""Diagnostic health checker (`doctor`) and safe deep-repair runner (`repair`)."""

from __future__ import annotations

import json
import shutil
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig
from agentsync.models.canonical import DoctorIssue, RepairResult
from agentsync.state.backup import BackupManager
from agentsync.utils.atomic_io import atomic_write_bytes, sqlite_integrity_check
from agentsync.utils.jsonc import loads_jsonc
from agentsync.utils.process_guard import assert_apps_closed


def _validate_config_bytes(path: Path, raw: bytes) -> bool:
    """Validate TOML / JSON / JSONC bytes before auto-restoring a corrupted config."""
    text = raw.decode("utf-8-sig", errors="strict")
    suf = path.suffix.lower()
    if suf == ".toml":
        tomllib.loads(text)
        return True
    if suf == ".jsonc":
        loads_jsonc(text)
        return True
    if suf == ".json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            loads_jsonc(text)
        return True
    return len(raw) > 0


class DoctorEngine:
    """Validates all enabled tools' configs/sessions and performs deep repair safely."""

    def __init__(self, config: AgentSyncConfig, adapters: list[ToolAdapter]) -> None:
        self.config = config
        self.adapters = adapters
        self.backup_mgr = BackupManager(config)

    def diagnose(self) -> list[DoctorIssue]:
        issues: list[DoctorIssue] = []

        # Verify agentsync's own state DB if it exists
        if self.config.state_db_path.exists():
            ok, detail = sqlite_integrity_check(self.config.state_db_path, readonly=True)
            if not ok:
                issues.append(
                    DoctorIssue(
                        tool="agentsync",
                        severity="error",
                        code="STATE_DB_CORRUPT",
                        path=str(self.config.state_db_path),
                        message=f"sync_state.db failed integrity_check: {detail}",
                        fixable=True,
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

    def _auto_restore_corrupt_configs(
        self,
        adapter: ToolAdapter,
        issues: list[DoctorIssue],
        *,
        dry_run: bool,
    ) -> RepairResult:
        """Restore corrupted JSON/JSONC/TOML config files from the latest verified agentsync backup."""
        res = RepairResult(tool=adapter.tool_name)
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        for iss in issues:
            if iss.tool != adapter.tool_name or not iss.path:
                continue
            code_u = iss.code.upper()
            if not any(tok in code_u for tok in ("INVALID_TOML", "INVALID_JSON", "INVALID_GLOBAL_STATE", "INVALID_JSONC")):
                continue
            target = Path(iss.path)
            if not target.exists():
                continue
            match = self.backup_mgr.find_latest_valid_file_in_backups(
                target, validator=lambda b, p=target: _validate_config_bytes(p, b)
            )
            if not match:
                continue
            b_id, valid_src = match
            if dry_run:
                res.fixed_count += 1
                res.details.append(
                    f"Would quarantine corrupted {target.name} and auto-restore valid copy from backup {b_id}"
                )
            else:
                q_dst = self.config.quarantine_dir / f"{ts}_corrupt_cfg" / adapter.tool_name / target.name
                q_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, q_dst)
                atomic_write_bytes(target, valid_src.read_bytes())
                res.quarantined_files.append(str(q_dst))
                res.fixed_count += 1
                res.details.append(
                    f"Quarantined corrupted {target.name} and auto-restored valid config from backup {b_id}"
                )
        return res

    def repair(
        self,
        dry_run: bool = False,
        force: bool = False,
        check_processes: bool = True,
    ) -> tuple[list[RepairResult], str | None]:
        if not dry_run and check_processes:
            assert_apps_closed(force=force, check_apps=[a.tool_name for a in self.adapters])

        pre_issues = self.diagnose()

        backup_id: str | None = None
        if not dry_run:
            # Skip already-corrupted DBs gracefully if needed? BackupManager snapshots intact files;
            # wrap defensively only if a target SQLite is un-openable, otherwise take full backup.
            try:
                manifest = self.backup_mgr.create_backup(self.adapters, reason="repair")
                backup_id = manifest.backup_id
            except Exception:
                backup_id = None

        results: list[RepairResult] = []
        for adapter in self.adapters:
            if not adapter.root_path.exists():
                continue
            cfg_res = self._auto_restore_corrupt_configs(adapter, pre_issues, dry_run=dry_run)
            res = adapter.repair(self.config.quarantine_dir, dry_run=dry_run)
            if cfg_res.fixed_count > 0:
                res.fixed_count += cfg_res.fixed_count
                res.quarantined_files.extend(cfg_res.quarantined_files)
                res.details = cfg_res.details + res.details
            if res.fixed_count > 0 or res.details:
                results.append(res)
        return results, backup_id
