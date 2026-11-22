"""Atomic file operations, syntax/schema validation prior to replace, and safe SQLite helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tomllib
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_temp_path(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return target.parent / f".{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"


def atomic_write_bytes(path: Path, data: bytes) -> bool:
    """Atomically write bytes to `path` via temp file + fsync + os.replace.

    Returns True if created or changed, False if already byte-identical.
    """
    if path.exists() and path.is_file():
        try:
            if path.read_bytes() == data:
                return False
        except OSError:
            pass

    tmp = _make_temp_path(path)
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except BaseException:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def atomic_write_text(
    path: Path,
    text: str,
    validator: Callable[[str], None] | None = None,
) -> bool:
    """Validate text, write UTF-8 (strictly without BOM, LF endings), re-validate, and replace."""
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n")
    if validator is not None:
        validator(normalized)

    data = normalized.encode("utf-8")
    if path.exists() and path.is_file():
        try:
            if path.read_bytes() == data:
                return False
        except OSError:
            pass

    tmp = _make_temp_path(path)
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if validator is not None:
            validator(tmp.read_text(encoding="utf-8"))
        os.replace(tmp, path)
        return True
    except BaseException:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def atomic_write_json(
    path: Path,
    payload: Any,
    schema_model: type[BaseModel] | None = None,
    sort_keys: bool = False,
) -> bool:
    """Serialize JSON, validate syntax and optional Pydantic model, then replace atomically."""
    if schema_model is not None:
        schema_model.model_validate(payload)

    rendered = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=sort_keys) + "\n"

    def _validate(raw: str) -> None:
        parsed = json.loads(raw)
        if schema_model is not None:
            schema_model.model_validate(parsed)

    return atomic_write_text(path, rendered, validator=_validate)


def atomic_write_toml(
    path: Path,
    payload: dict[str, Any],
    schema_model: type[BaseModel] | None = None,
) -> bool:
    """Serialize TOML, parse back with tomllib to verify validity, then replace atomically."""
    if schema_model is not None:
        schema_model.model_validate(payload)

    rendered = tomli_w.dumps(payload)

    def _validate(raw: str) -> None:
        parsed = tomllib.loads(raw)
        if schema_model is not None:
            schema_model.model_validate(parsed)

    return atomic_write_text(path, rendered, validator=_validate)


def sqlite_safe_connect(
    db_path: Path,
    readonly: bool = False,
    *,
    timeout: float = 10.0,
    allow_immutable_fallback: bool = True,
) -> sqlite3.Connection:
    """Open a SQLite database safely with busy_timeout and WAL-aware readonly handling."""
    busy_ms = max(5000, int(timeout * 1000))
    if readonly:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=timeout)
            conn.execute(f"PRAGMA busy_timeout = {busy_ms}")
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.OperationalError:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            wal_path = Path(f"{db_path}-wal")
            # Never silently skip a non-empty WAL via immutable=1 if caller forbids it
            if not allow_immutable_fallback or (wal_path.exists() and wal_path.stat().st_size > 0):
                raise
            imm_uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
            imm_conn = sqlite3.connect(imm_uri, uri=True, timeout=timeout)
            try:
                imm_conn.execute("PRAGMA query_only = ON")
                return imm_conn
            except BaseException:
                try:
                    imm_conn.close()
                except Exception:
                    pass
                raise
        except BaseException:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            raise

    db_path.parent.mkdir(parents=True, exist_ok=True)
    rw_conn = sqlite3.connect(str(db_path), timeout=timeout)
    try:
        rw_conn.execute(f"PRAGMA busy_timeout = {busy_ms}")
        rw_conn.execute("PRAGMA foreign_keys = ON")
        return rw_conn
    except BaseException:
        try:
            rw_conn.close()
        except Exception:
            pass
        raise


def sqlite_integrity_check(db_path: Path, *, readonly: bool = True) -> tuple[bool, str]:
    """Run PRAGMA integrity_check on `db_path`; returns (is_ok, detail_message)."""
    if not db_path.exists():
        return False, f"Database file does not exist: {db_path}"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite_safe_connect(db_path, readonly=readonly, allow_immutable_fallback=False)
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        msgs = [str(r[0]) for r in rows if r]
        if msgs == ["ok"]:
            return True, "ok"
        return False, "; ".join(msgs) or "unknown integrity_check failure"
    except Exception as exc:
        return False, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def sqlite_wal_checkpoint(
    db_path: Path,
    mode: str = "TRUNCATE",
) -> tuple[bool, str]:
    """Run PRAGMA wal_checkpoint(mode) on a writable SQLite DB to flush WAL frames safely."""
    if not db_path.exists():
        return False, f"Database file does not exist: {db_path}"
    allowed_modes = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}
    chosen = mode.upper() if mode.upper() in allowed_modes else "TRUNCATE"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite_safe_connect(db_path, readonly=False, timeout=10.0)
        jm_row = conn.execute("PRAGMA journal_mode").fetchone()
        jm = str(jm_row[0]).lower() if jm_row else ""
        if jm != "wal":
            return True, f"journal_mode={jm or 'delete'} (no WAL checkpoint needed)"
        res = conn.execute(f"PRAGMA wal_checkpoint({chosen})").fetchone()
        busy = int(res[0]) if res and len(res) >= 1 else 0
        log_frames = int(res[1]) if res and len(res) >= 2 else 0
        ckpt_frames = int(res[2]) if res and len(res) >= 3 else 0
        if busy != 0:
            return False, f"WAL checkpoint blocked (busy={busy}, log={log_frames}, checkpointed={ckpt_frames})"
        return True, f"checkpointed {ckpt_frames}/{log_frames} WAL frame(s)"
    except Exception as exc:
        return False, str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def sqlite_cleanup_sidecars(db_path: Path) -> list[str]:
    """Remove SQLite -wal, -shm, and -journal sidecar files adjacent to `db_path`."""
    removed: list[str] = []
    for suf in ("-wal", "-shm", "-journal"):
        sc = Path(f"{db_path}{suf}")
        if sc.exists():
            sc.unlink()
            removed.append(str(sc))
    return removed


def sqlite_backup_file(
    src: Path,
    dst: Path,
    *,
    verify_integrity: bool = True,
) -> None:
    """Safely snapshot a SQLite DB (including live WAL frames) into `dst` atomically and verify integrity."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _make_temp_path(dst)
    src_conn: sqlite3.Connection | None = None
    dst_conn: sqlite3.Connection | None = None
    try:
        src_conn = sqlite_safe_connect(src, readonly=True, timeout=10.0, allow_immutable_fallback=False)
        dst_conn = sqlite3.connect(str(tmp), timeout=10.0)
        dst_conn.execute("PRAGMA busy_timeout = 5000")
        src_conn.backup(dst_conn)
        dst_conn.commit()
        if verify_integrity:
            chk_rows = dst_conn.execute("PRAGMA integrity_check").fetchall()
            chk_msgs = [str(r[0]) for r in chk_rows if r]
            if chk_msgs != ["ok"]:
                raise sqlite3.DatabaseError(
                    f"Backup snapshot integrity_check failed for {src}: {'; '.join(chk_msgs)}"
                )
        dst_conn.close()
        dst_conn = None
        src_conn.close()
        src_conn = None
        os.replace(tmp, dst)
    except BaseException:
        if dst_conn is not None:
            try:
                dst_conn.close()
            except Exception:
                pass
        if src_conn is not None:
            try:
                src_conn.close()
            except Exception:
                pass
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def sqlite_restore_from_snapshot(snapshot_path: Path, target_db: Path) -> None:
    """Verify a SQLite snapshot, clear stale WAL/SHM sidecars, replace target atomically, and verify again."""
    ok, msg = sqlite_integrity_check(snapshot_path, readonly=True)
    if not ok:
        raise sqlite3.DatabaseError(f"Refusing to restore corrupt SQLite snapshot {snapshot_path}: {msg}")
    data = snapshot_path.read_bytes()
    sqlite_cleanup_sidecars(target_db)
    atomic_write_bytes(target_db, data)
    ok_after, msg_after = sqlite_integrity_check(target_db, readonly=True)
    if not ok_after:
        raise sqlite3.DatabaseError(f"Restored SQLite DB {target_db} failed integrity_check: {msg_after}")


def sqlite_transactional_edit(
    db_path: Path,
    mutator: Callable[[sqlite3.Connection], Any],
    *,
    ddl: str | None = None,
    checkpoint: bool = True,
) -> Any:
    """Snapshot -> mutate -> checkpoint WAL -> verify PRAGMA integrity_check -> auto-rollback on error.

    Prevents stale WAL/SHM frames or interrupted writes from ever causing `disk I/O error` in host apps.
    """
    existed = db_path.exists()
    snapshot_path: Path | None = None
    if existed:
        ok_pre, msg_pre = sqlite_integrity_check(db_path, readonly=True)
        if not ok_pre:
            raise sqlite3.DatabaseError(
                f"Pre-write integrity_check failed for {db_path}: {msg_pre}. Run 'agentsync repair' first."
            )
        snapshot_path = _make_temp_path(db_path.with_suffix(db_path.suffix + ".prewrite"))
        sqlite_backup_file(db_path, snapshot_path, verify_integrity=True)

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite_safe_connect(db_path, readonly=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        if ddl:
            conn.executescript(ddl)
            conn.commit()
        result = mutator(conn)
        conn.commit()
        if checkpoint:
            jm_row = conn.execute("PRAGMA journal_mode").fetchone()
            if jm_row and str(jm_row[0]).lower() == "wal":
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        chk_rows = conn.execute("PRAGMA integrity_check").fetchall()
        chk_msgs = [str(r[0]) for r in chk_rows if r]
        conn.close()
        conn = None
        if chk_msgs != ["ok"]:
            raise sqlite3.DatabaseError(
                f"Post-write integrity_check failed for {db_path}: {'; '.join(chk_msgs)}"
            )
        return result
    except BaseException:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        if snapshot_path is not None and snapshot_path.exists():
            try:
                sqlite_restore_from_snapshot(snapshot_path, db_path)
            except Exception:
                pass
        elif not existed and db_path.exists():
            try:
                sqlite_cleanup_sidecars(db_path)
                db_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if snapshot_path is not None and snapshot_path.exists():
            try:
                snapshot_path.unlink()
            except OSError:
                pass


def sync_directory_files(target_dir: Path, files: dict[str, bytes]) -> bool:
    """Synchronize `target_dir` so it matches `files` (rel_path -> bytes) with zero writes if identical."""
    changed = False
    expected_rel_paths = {p.replace("\\", "/") for p in files}

    if not target_dir.exists():
        target_dir.mkdir(parents=True, exist_ok=True)
        changed = True

    for existing in list(target_dir.rglob("*")):
        if existing.is_file():
            rel = existing.relative_to(target_dir).as_posix()
            if rel not in expected_rel_paths:
                existing.unlink()
                changed = True

    for rel, content in sorted(files.items()):
        dest = target_dir / Path(rel)
        if atomic_write_bytes(dest, content):
            changed = True

    for existing_dir in sorted(
        (p for p in target_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            if not any(existing_dir.iterdir()):
                existing_dir.rmdir()
                changed = True
        except OSError:
            pass

    return changed


def remove_tree_if_exists(path: Path) -> bool:
    """Remove a directory tree or file if present; returns True if anything was removed."""
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or path.is_file():
        path.unlink()
        return True
    shutil.rmtree(path)
    return True
