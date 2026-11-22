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


def sqlite_safe_connect(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    """Open a SQLite database safely; tolerate locked WAL files in readonly mode."""
    if readonly:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3.0)
            conn.execute("PRAGMA query_only = ON")
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.OperationalError:
            imm_uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
            return sqlite3.connect(imm_uri, uri=True, timeout=3.0)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def sqlite_backup_file(src: Path, dst: Path) -> None:
    """Safely snapshot a SQLite DB (including WAL state) into `dst` atomically."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _make_temp_path(dst)
    src_conn: sqlite3.Connection | None = None
    dst_conn: sqlite3.Connection | None = None
    try:
        src_conn = sqlite_safe_connect(src, readonly=True)
        dst_conn = sqlite3.connect(str(tmp))
        src_conn.backup(dst_conn)
        dst_conn.commit()
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
