"""Multi-tool SQLite state store (`sync_state.db`) for mappings, hashes, runs, and conflicts."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agentsync.utils.atomic_io import sqlite_safe_connect

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_states (
    category TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    native_id TEXT,
    content_hash TEXT,
    last_synced_hash TEXT,
    updated_at_ms INTEGER NOT NULL DEFAULT 0,
    last_synced_at_ms INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (category, canonical_key, tool_name)
);

CREATE INDEX IF NOT EXISTS idx_entity_cat_key ON entity_states(category, canonical_key);

CREATE TABLE IF NOT EXISTS project_states (
    canonical_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    native_id TEXT,
    secondary_id TEXT,
    display_path TEXT NOT NULL,
    name TEXT NOT NULL,
    last_synced_hash TEXT,
    updated_at_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (canonical_key, tool_name)
);

CREATE TABLE IF NOT EXISTS chat_canonical (
    canonical_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    project_key TEXT,
    title TEXT NOT NULL DEFAULT '',
    last_synced_hash TEXT,
    last_synced_at_ms INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chat_canonical_fp ON chat_canonical(fingerprint);

CREATE TABLE IF NOT EXISTS chat_links (
    canonical_id TEXT NOT NULL REFERENCES chat_canonical(canonical_id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    native_chat_id TEXT NOT NULL,
    content_hash TEXT,
    updated_at_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (canonical_id, tool_name),
    UNIQUE (tool_name, native_chat_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_links_tool_native ON chat_links(tool_name, native_chat_id);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER,
    dry_run INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    backup_id TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conflicts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    category TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    winner_tool TEXT NOT NULL,
    loser_tool TEXT NOT NULL,
    backup_path TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    details TEXT NOT NULL DEFAULT ''
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class EntityStateRecord(BaseModel):
    category: str
    canonical_key: str
    tool_name: str
    native_id: str | None = None
    content_hash: str | None = None
    last_synced_hash: str | None = None
    updated_at_ms: int = 0
    last_synced_at_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectStateRecord(BaseModel):
    canonical_key: str
    tool_name: str
    native_id: str | None = None
    secondary_id: str | None = None
    display_path: str
    name: str
    last_synced_hash: str | None = None
    updated_at_ms: int = 0


class ChatLinkRecord(BaseModel):
    canonical_id: str
    tool_name: str
    native_chat_id: str
    content_hash: str | None = None
    updated_at_ms: int = 0


class ChatCanonicalRecord(BaseModel):
    canonical_id: str
    fingerprint: str
    project_key: str | None = None
    title: str = ""
    last_synced_hash: str | None = None
    last_synced_at_ms: int = 0
    links: dict[str, ChatLinkRecord] = Field(default_factory=dict)


class StateStore:
    """Multi-tool SQLite persistence layer for sync mappings, hashes, and conflict logs."""

    def __init__(self, db_path: Path, readonly: bool = False) -> None:
        self.db_path = db_path
        self.readonly = readonly
        self._conn: sqlite3.Connection | None = None
        if not readonly:
            self._ensure_initialized()
        elif db_path.exists():
            self._conn = sqlite_safe_connect(db_path, readonly=True)
            self._conn.row_factory = sqlite3.Row

    def _ensure_initialized(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite_safe_connect(self.db_path, readonly=False)
        conn.row_factory = sqlite3.Row
        conn.executescript(_DDL)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _has_table(self, name: str) -> bool:
        if self._conn is None:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    # --- Entity states ---

    def get_entity_states(self, category: str, canonical_key: str) -> dict[str, EntityStateRecord]:
        if not self._has_table("entity_states"):
            return {}
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM entity_states WHERE category = ? AND canonical_key = ?",
            (category, canonical_key),
        ).fetchall()
        out: dict[str, EntityStateRecord] = {}
        for r in rows:
            out[r["tool_name"]] = EntityStateRecord(
                category=r["category"],
                canonical_key=r["canonical_key"],
                tool_name=r["tool_name"],
                native_id=r["native_id"],
                content_hash=r["content_hash"],
                last_synced_hash=r["last_synced_hash"],
                updated_at_ms=r["updated_at_ms"],
                last_synced_at_ms=r["last_synced_at_ms"],
                metadata=json.loads(r["metadata_json"] or "{}"),
            )
        return out

    def upsert_entity_state(self, rec: EntityStateRecord) -> None:
        assert self._conn is not None and not self.readonly
        self._conn.execute(
            """
            INSERT INTO entity_states(
                category, canonical_key, tool_name, native_id,
                content_hash, last_synced_hash, updated_at_ms, last_synced_at_ms, metadata_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(category, canonical_key, tool_name) DO UPDATE SET
                native_id=excluded.native_id,
                content_hash=excluded.content_hash,
                last_synced_hash=excluded.last_synced_hash,
                updated_at_ms=excluded.updated_at_ms,
                last_synced_at_ms=excluded.last_synced_at_ms,
                metadata_json=excluded.metadata_json
            """,
            (
                rec.category,
                rec.canonical_key,
                rec.tool_name,
                rec.native_id,
                rec.content_hash,
                rec.last_synced_hash,
                rec.updated_at_ms,
                rec.last_synced_at_ms or now_ms(),
                json.dumps(rec.metadata, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def clear_category(self, category: str, tool_name: str | None = None) -> None:
        assert self._conn is not None and not self.readonly
        if tool_name:
            self._conn.execute(
                "DELETE FROM entity_states WHERE category = ? AND tool_name = ?",
                (category, tool_name),
            )
        else:
            self._conn.execute("DELETE FROM entity_states WHERE category = ?", (category,))
        self._conn.commit()

    # --- Project states ---

    def list_project_states(self) -> dict[str, dict[str, ProjectStateRecord]]:
        if not self._has_table("project_states"):
            return {}
        assert self._conn is not None
        rows = self._conn.execute("SELECT * FROM project_states").fetchall()
        grouped: dict[str, dict[str, ProjectStateRecord]] = {}
        for r in rows:
            rec = ProjectStateRecord(**dict(r))
            grouped.setdefault(rec.canonical_key, {})[rec.tool_name] = rec
        return grouped

    def upsert_project_state(self, rec: ProjectStateRecord) -> None:
        assert self._conn is not None and not self.readonly
        self._conn.execute(
            """
            INSERT INTO project_states(
                canonical_key, tool_name, native_id, secondary_id,
                display_path, name, last_synced_hash, updated_at_ms
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_key, tool_name) DO UPDATE SET
                native_id=excluded.native_id,
                secondary_id=excluded.secondary_id,
                display_path=excluded.display_path,
                name=excluded.name,
                last_synced_hash=excluded.last_synced_hash,
                updated_at_ms=excluded.updated_at_ms
            """,
            (
                rec.canonical_key,
                rec.tool_name,
                rec.native_id,
                rec.secondary_id,
                rec.display_path,
                rec.name,
                rec.last_synced_hash,
                rec.updated_at_ms or now_ms(),
            ),
        )
        self._conn.commit()

    # --- Chat canonical & links ---

    def list_chats(self) -> list[ChatCanonicalRecord]:
        if not self._has_table("chat_canonical"):
            return []
        assert self._conn is not None
        canon_rows = self._conn.execute(
            "SELECT * FROM chat_canonical ORDER BY last_synced_at_ms DESC"
        ).fetchall()
        link_rows = self._conn.execute("SELECT * FROM chat_links").fetchall()
        links_by_cid: dict[str, dict[str, ChatLinkRecord]] = {}
        for lr in link_rows:
            rec = ChatLinkRecord(**dict(lr))
            links_by_cid.setdefault(rec.canonical_id, {})[rec.tool_name] = rec
        out: list[ChatCanonicalRecord] = []
        for cr in canon_rows:
            cid = cr["canonical_id"]
            out.append(ChatCanonicalRecord(**dict(cr), links=links_by_cid.get(cid, {})))
        return out

    def upsert_chat(self, rec: ChatCanonicalRecord) -> None:
        assert self._conn is not None and not self.readonly
        self._conn.execute(
            """
            INSERT INTO chat_canonical(
                canonical_id, fingerprint, project_key, title, last_synced_hash, last_synced_at_ms
            ) VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_id) DO UPDATE SET
                fingerprint=excluded.fingerprint,
                project_key=excluded.project_key,
                title=excluded.title,
                last_synced_hash=excluded.last_synced_hash,
                last_synced_at_ms=excluded.last_synced_at_ms
            """,
            (
                rec.canonical_id,
                rec.fingerprint,
                rec.project_key,
                rec.title,
                rec.last_synced_hash,
                rec.last_synced_at_ms or now_ms(),
            ),
        )
        for tool_name, link in rec.links.items():
            # Clean any stale binding of this (tool_name, native_chat_id) on another canonical_id
            self._conn.execute(
                "DELETE FROM chat_links WHERE tool_name = ? AND native_chat_id = ? AND canonical_id != ?",
                (tool_name, link.native_chat_id, rec.canonical_id),
            )
            self._conn.execute(
                """
                INSERT INTO chat_links(canonical_id, tool_name, native_chat_id, content_hash, updated_at_ms)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(canonical_id, tool_name) DO UPDATE SET
                    native_chat_id=excluded.native_chat_id,
                    content_hash=excluded.content_hash,
                    updated_at_ms=excluded.updated_at_ms
                """,
                (
                    rec.canonical_id,
                    tool_name,
                    link.native_chat_id,
                    link.content_hash,
                    link.updated_at_ms,
                ),
            )
        self._conn.commit()

    # --- Runs & conflicts ---

    def record_run(
        self,
        run_id: str,
        mode: str,
        dry_run: bool,
        status: str,
        summary: dict[str, Any],
        backup_id: str | None = None,
        started_at_ms: int | None = None,
    ) -> None:
        if self._conn is None or self.readonly:
            return
        now = now_ms()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO sync_runs(
                run_id, mode, started_at_ms, finished_at_ms, dry_run, status, backup_id, summary_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                mode,
                started_at_ms or now,
                now,
                1 if dry_run else 0,
                status,
                backup_id,
                json.dumps(summary, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def record_conflict(
        self,
        run_id: str,
        category: str,
        entity_id: str,
        winner_tool: str,
        loser_tool: str,
        backup_path: str,
        details: str = "",
    ) -> None:
        if self._conn is None or self.readonly:
            return
        self._conn.execute(
            """
            INSERT INTO conflicts_log(
                run_id, category, entity_id, winner_tool, loser_tool, backup_path, created_at_ms, details
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, category, entity_id, winner_tool, loser_tool, backup_path, now_ms(), details),
        )
        self._conn.commit()

    def list_conflicts(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._has_table("conflicts_log"):
            return []
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM conflicts_log ORDER BY created_at_ms DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        if not self._has_table("sync_runs"):
            return []
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT * FROM sync_runs ORDER BY started_at_ms DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_summary_counts(self) -> dict[str, int]:
        counts = {
            "projects": 0,
            "chats": 0,
            "skills": 0,
            "mcp": 0,
            "plugins": 0,
            "instructions": 0,
            "conflicts": 0,
        }
        if self._conn is None:
            return counts
        if self._has_table("project_states"):
            counts["projects"] = self._conn.execute(
                "SELECT COUNT(DISTINCT canonical_key) FROM project_states"
            ).fetchone()[0]
        if self._has_table("chat_canonical"):
            counts["chats"] = self._conn.execute(
                "SELECT COUNT(*) FROM chat_canonical"
            ).fetchone()[0]
        if self._has_table("conflicts_log"):
            counts["conflicts"] = self._conn.execute(
                "SELECT COUNT(*) FROM conflicts_log"
            ).fetchone()[0]
        if self._has_table("entity_states"):
            for cat in ("skills", "mcp", "plugins", "instructions"):
                counts[cat] = self._conn.execute(
                    "SELECT COUNT(DISTINCT canonical_key) FROM entity_states WHERE category = ?",
                    (cat,),
                ).fetchone()[0]
        return counts
