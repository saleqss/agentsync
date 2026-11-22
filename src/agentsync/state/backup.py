"""Versioned SHA-256 verified full backup and atomic restore across all enabled tools."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from agentsync.config import AgentSyncConfig
from agentsync.utils.atomic_io import (
    atomic_write_bytes,
    atomic_write_json,
    remove_tree_if_exists,
    sha256_file,
    sqlite_backup_file,
    sqlite_integrity_check,
    sqlite_restore_from_snapshot,
)
from agentsync.utils.process_guard import assert_apps_closed

if TYPE_CHECKING:
    from agentsync.adapters.base import ToolAdapter


class BackupEntry(BaseModel):
    original_path: str
    backup_rel_path: str
    kind: Literal["file", "sqlite"]
    sha256: str
    size_bytes: int


class BackupManifest(BaseModel):
    backup_id: str
    created_at_utc: str
    reason: str
    tools: list[str] = Field(default_factory=list)
    entries: list[BackupEntry] = Field(default_factory=list)
    absent_files: list[str] = Field(default_factory=list)
    tracked_dirs: list[str] = Field(default_factory=list)
    tracked_dir_files: dict[str, list[str]] = Field(default_factory=dict)


class RestoreResult(BaseModel):
    backup_id: str
    restored_files: int = 0
    removed_files: int = 0
    warnings: list[str] = Field(default_factory=list)


class BackupVerificationReport(BaseModel):
    backup_id: str
    ok: bool = True
    checked_files: int = 0
    checked_sqlite: int = 0
    total_bytes: int = 0
    errors: list[str] = Field(default_factory=list)


class BackupManager:
    """Creates consistent snapshots (including WAL SQLite DBs) and restores exact pre-sync state."""

    def __init__(self, config: AgentSyncConfig) -> None:
        self.config = config

    def create_backup(
        self,
        adapters: list[ToolAdapter],
        reason: str = "manual",
        extra_project_paths: list[Path] | None = None,
    ) -> BackupManifest:
        ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup_id = f"{ts}-{uuid.uuid4().hex[:6]}"
        backup_root = self.config.backups_dir / backup_id
        payload_root = backup_root / "files"
        payload_root.mkdir(parents=True, exist_ok=True)

        entries: list[BackupEntry] = []
        absent_files: list[str] = []
        tracked_dir_files: dict[str, list[str]] = {}
        tracked_dir_strs: list[str] = []
        seen_paths: set[str] = set()
        counter = 0

        def _backup_one(src: Path, kind: Literal["file", "sqlite"]) -> None:
            nonlocal counter
            key = os.path.normcase(os.path.abspath(str(src)))
            if key in seen_paths:
                return
            seen_paths.add(key)

            if not src.exists():
                absent_files.append(str(src))
                return

            counter += 1
            safe_name = f"{counter:05d}_{src.name}"
            rel = f"files/{safe_name}"
            dst = backup_root / rel
            if kind == "sqlite":
                sqlite_backup_file(src, dst)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            entries.append(
                BackupEntry(
                    original_path=str(src),
                    backup_rel_path=rel,
                    kind=kind,
                    sha256=sha256_file(dst),
                    size_bytes=dst.stat().st_size,
                )
            )

        # Back up agentsync's own state DB
        _backup_one(self.config.state_db_path, "sqlite")

        # Back up project-level instruction files across all known workspaces
        if extra_project_paths:
            for proj in extra_project_paths:
                if proj.exists() and proj.is_dir():
                    for fname in ("AGENTS.md", "GEMINI.md", "CLAUDE.md", ".mcp.json"):
                        _backup_one(proj / fname, "file")

        # Collect tool-specific backup targets from each adapter
        for adapter in adapters:
            files, dirs = adapter.backup_targets()
            for fpath, fkind in files:
                _backup_one(fpath, fkind)
            for tdir in dirs:
                tdir_key = str(tdir)
                if tdir_key in tracked_dir_files:
                    continue
                tracked_dir_strs.append(tdir_key)
                dir_file_list: list[str] = []
                if tdir.exists() and tdir.is_dir():
                    for item in sorted(tdir.rglob("*")):
                        if not item.is_file():
                            continue
                        if adapter.should_skip_backup_file(tdir, item):
                            continue
                        dir_file_list.append(str(item))
                        kind: Literal["file", "sqlite"] = (
                            "sqlite" if adapter.is_sqlite_backup_file(tdir, item) else "file"
                        )
                        _backup_one(item, kind)
                tracked_dir_files[tdir_key] = dir_file_list

        manifest = BackupManifest(
            backup_id=backup_id,
            created_at_utc=datetime.now(UTC).isoformat(),
            reason=reason,
            tools=[a.tool_name for a in adapters],
            entries=entries,
            absent_files=absent_files,
            tracked_dirs=tracked_dir_strs,
            tracked_dir_files=tracked_dir_files,
        )
        atomic_write_json(backup_root / "manifest.json", manifest.model_dump(), BackupManifest)
        self.prune_old_backups()
        return manifest

    def prune_old_backups(
        self,
        max_backups: int | None = None,
        max_days: int | None = None,
    ) -> list[str]:
        """Enforce backup retention policy by count and age (always keeps at least the newest backup)."""
        limit_count = max(1, max_backups if max_backups is not None else self.config.max_backups)
        limit_days = max(1, max_days if max_days is not None else self.config.max_backup_days)
        manifests = self.list_backups()
        if len(manifests) <= 1:
            return []

        cutoff = datetime.now(UTC) - timedelta(days=limit_days)
        removed: list[str] = []
        for idx, mf in enumerate(manifests):
            if idx == 0:
                continue  # Never prune the single newest snapshot
            too_many = idx >= limit_count
            too_old = False
            try:
                created_dt = datetime.fromisoformat(mf.created_at_utc.replace("Z", "+00:00"))
                if created_dt < cutoff:
                    too_old = True
            except Exception:
                pass
            if too_many or too_old:
                bdir = self.config.backups_dir / mf.backup_id
                if remove_tree_if_exists(bdir):
                    removed.append(mf.backup_id)
        return removed

    def verify_backup(self, backup_id: str) -> BackupVerificationReport:
        """Verify SHA-256 digests for all entries and PRAGMA integrity_check for all SQLite snapshots."""
        report = BackupVerificationReport(backup_id=backup_id)
        backup_root = self.config.backups_dir / backup_id
        manifest_path = backup_root / "manifest.json"
        if not manifest_path.exists():
            report.ok = False
            report.errors.append(f"Missing manifest.json at {backup_root}")
            return report

        try:
            manifest = BackupManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            report.ok = False
            report.errors.append(f"Invalid manifest JSON: {exc}")
            return report

        for entry in manifest.entries:
            src = backup_root / entry.backup_rel_path
            if not src.exists():
                report.ok = False
                report.errors.append(f"Missing payload: {entry.backup_rel_path}")
                continue
            actual = sha256_file(src)
            report.checked_files += 1
            report.total_bytes += src.stat().st_size
            if actual != entry.sha256:
                report.ok = False
                report.errors.append(
                    f"SHA-256 mismatch for {entry.backup_rel_path} ({Path(entry.original_path).name})"
                )
                continue
            if entry.kind == "sqlite":
                report.checked_sqlite += 1
                ok_db, msg_db = sqlite_integrity_check(src, readonly=True)
                if not ok_db:
                    report.ok = False
                    report.errors.append(
                        f"SQLite integrity_check failed for {Path(entry.original_path).name}: {msg_db}"
                    )
        return report

    def verify_all_backups(self) -> list[BackupVerificationReport]:
        """Verify every stored backup snapshot and return per-backup reports."""
        return [self.verify_backup(mf.backup_id) for mf in self.list_backups()]

    def total_backup_bytes(self) -> int:
        """Return total disk footprint in bytes across all stored backups."""
        if not self.config.backups_dir.exists():
            return 0
        return sum(p.stat().st_size for p in self.config.backups_dir.rglob("*") if p.is_file())

    def find_latest_valid_file_in_backups(
        self,
        original_path: Path,
        validator: Callable[[bytes], bool] | None = None,
    ) -> tuple[str, Path] | None:
        """Locate the most recent SHA-256 verified backup payload for `original_path` that passes `validator`."""
        target_norm = os.path.normcase(os.path.abspath(str(original_path)))
        for mf in self.list_backups():
            backup_root = self.config.backups_dir / mf.backup_id
            for entry in mf.entries:
                if os.path.normcase(os.path.abspath(entry.original_path)) != target_norm:
                    continue
                src = backup_root / entry.backup_rel_path
                if not src.exists():
                    continue
                if sha256_file(src) != entry.sha256:
                    continue
                if validator is not None:
                    try:
                        if not validator(src.read_bytes()):
                            continue
                    except Exception:
                        continue
                return mf.backup_id, src
        return None

    def list_backups(self) -> list[BackupManifest]:
        if not self.config.backups_dir.exists():
            return []
        manifests: list[BackupManifest] = []
        for d in sorted(self.config.backups_dir.iterdir(), reverse=True):
            mf = d / "manifest.json"
            if d.is_dir() and mf.exists():
                try:
                    manifests.append(
                        BackupManifest.model_validate_json(mf.read_text(encoding="utf-8"))
                    )
                except Exception:
                    continue
        return manifests

    def restore_backup(
        self,
        backup_id: str,
        adapters_by_name: dict[str, ToolAdapter] | None = None,
        force: bool = False,
        check_processes: bool = True,
    ) -> RestoreResult:
        backup_root = self.config.backups_dir / backup_id
        manifest_path = backup_root / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Backup '{backup_id}' not found at {backup_root}")

        manifest = BackupManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))

        if check_processes:
            assert_apps_closed(force=force, check_apps=manifest.tools or None)

        # Phase 1: Verify SHA-256 of every snapshot file before mutating disk
        for entry in manifest.entries:
            src = backup_root / entry.backup_rel_path
            if not src.exists():
                raise FileNotFoundError(f"Corrupted backup: missing payload {src}")
            actual = sha256_file(src)
            if actual != entry.sha256:
                raise ValueError(
                    f"Corrupted backup ({entry.backup_rel_path}): "
                    f"expected SHA-256 {entry.sha256}, got {actual}"
                )

        result = RestoreResult(backup_id=backup_id)

        # Phase 2: Delete files created after the backup was taken
        for absent in manifest.absent_files:
            ap = Path(absent)
            if ap.exists() and ap.is_file():
                ap.unlink()
                result.removed_files += 1
                for suf in ("-wal", "-shm", "-journal"):
                    sc = Path(str(ap) + suf)
                    if sc.exists():
                        sc.unlink()

        adapter_list = list((adapters_by_name or {}).values())
        for tdir_str, original_list in manifest.tracked_dir_files.items():
            tdir = Path(tdir_str)
            if not tdir.exists() or not tdir.is_dir():
                continue
            allowed = {os.path.normcase(os.path.abspath(p)) for p in original_list}
            for item in sorted(tdir.rglob("*")):
                if not item.is_file():
                    continue
                if item.name.endswith(("-wal", "-shm", "-journal")):
                    continue
                if any(a.should_skip_backup_file(tdir, item) for a in adapter_list):
                    continue
                norm_item = os.path.normcase(os.path.abspath(str(item)))
                if norm_item not in allowed:
                    item.unlink()
                    result.removed_files += 1
                    for suf in ("-wal", "-shm", "-journal"):
                        sc = Path(str(item) + suf)
                        if sc.exists():
                            sc.unlink()
            for sub in sorted(
                (d for d in tdir.rglob("*") if d.is_dir()),
                key=lambda d: len(d.parts),
                reverse=True,
            ):
                if sub.name == ".system":
                    continue
                try:
                    if not any(sub.iterdir()):
                        sub.rmdir()
                except OSError:
                    pass

        # Phase 3: Atomically restore every backed-up file (with WAL/SHM cleanup + integrity verify for SQLite)
        for entry in manifest.entries:
            src = backup_root / entry.backup_rel_path
            dst = Path(entry.original_path)
            if entry.kind == "sqlite":
                sqlite_restore_from_snapshot(src, dst)
            else:
                atomic_write_bytes(dst, src.read_bytes())
            result.restored_files += 1

        return result
