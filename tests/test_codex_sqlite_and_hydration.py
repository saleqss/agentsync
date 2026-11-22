"""Golden & regression tests for Codex WAL/SHM SQLite safety, rollback, process guard, hydration, and Deep Repair."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentsync.adapters.codex import CodexAdapter, validate_codex_rollout_file
from agentsync.config import AgentSyncConfig, ToolConfig
from agentsync.engine.doctor import DoctorEngine
from agentsync.models.canonical import ChatMessage, ChatSession, ProjectItem
from agentsync.state.backup import BackupManager
from agentsync.utils.atomic_io import (
    sqlite_backup_file,
    sqlite_integrity_check,
    sqlite_transactional_edit,
    sqlite_wal_checkpoint,
)
from agentsync.utils.process_guard import classify_process


def _create_codex_catalog_db(db_path: Path) -> None:
    """Create a real WAL-mode sqlite/codex.db matching Codex Desktop's local_thread_catalog schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE local_thread_catalog (
            host_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            display_title TEXT NOT NULL,
            source_created_at REAL NOT NULL,
            source_updated_at REAL NOT NULL,
            cwd TEXT,
            source_kind TEXT NOT NULL,
            source_detail TEXT,
            model_provider TEXT,
            git_branch TEXT,
            observation_sequence INTEGER NOT NULL,
            missing_candidate INTEGER NOT NULL DEFAULT 0,
            thread_source TEXT,
            source_recency_at REAL NOT NULL DEFAULT 0,
            pending_observed_title INTEGER NOT NULL DEFAULT 0,
            project_id TEXT,
            PRIMARY KEY (host_id, thread_id)
        );
        CREATE TABLE local_thread_catalog_metadata (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            catalog_revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE local_thread_catalog_sync_state (
            host_id TEXT PRIMARY KEY,
            watermark_updated_at REAL,
            initial_build_complete INTEGER NOT NULL DEFAULT 0,
            observation_sequence INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO local_thread_catalog_metadata(id, catalog_revision) VALUES(1, 2);
        INSERT INTO local_thread_catalog_sync_state(host_id, observation_sequence) VALUES('local', 1);
        """
    )
    conn.commit()
    conn.close()


def test_sqlite_wal_backup_checkpoint_and_automatic_rollback(tmp_path: Path) -> None:
    """WAL-mode SQLite DB with uncheckpointed data backs up cleanly and rolls back automatically on write failure."""
    db_path = tmp_path / "codex.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO kv VALUES ('keep', 'original_value')")
    conn.commit()

    # Keep WAL uncheckpointed while taking a verified snapshot
    wal = Path(f"{db_path}-wal")
    assert wal.exists() and wal.stat().st_size > 0
    snap = tmp_path / "snapshot.db"
    sqlite_backup_file(db_path, snap, verify_integrity=True)
    conn.close()

    # Verify snapshot captured WAL-resident rows and passes integrity_check
    ok_snap, msg_snap = sqlite_integrity_check(snap, readonly=True)
    assert ok_snap, msg_snap
    sc = sqlite3.connect(str(snap))
    assert sc.execute("SELECT v FROM kv WHERE k = 'keep'").fetchone() == ("original_value",)
    sc.close()

    # Verify transactional edit rolls back cleanly if mutator raises mid-write
    def _failing_mutator(c: sqlite3.Connection) -> None:
        c.execute("UPDATE kv SET v = 'corrupted_partial' WHERE k = 'keep'")
        c.commit()
        raise RuntimeError("Simulated mid-write crash")

    with pytest.raises(RuntimeError, match="Simulated mid-write crash"):
        sqlite_transactional_edit(db_path, _failing_mutator)

    ok_live, _ = sqlite_integrity_check(db_path, readonly=True)
    assert ok_live
    lc = sqlite3.connect(str(db_path))
    assert lc.execute("SELECT v FROM kv WHERE k = 'keep'").fetchone() == ("original_value",)
    lc.close()

    # Verify WAL checkpoint TRUNCATE succeeds cleanly
    ckpt_ok, _ = sqlite_wal_checkpoint(db_path, mode="TRUNCATE")
    assert ckpt_ok


def test_complete_process_guard_catches_all_codex_and_antigravity_helpers() -> None:
    """Process guard catches ChatGPT.exe, extension-host, runtime node, WebView, WSL codex, and excludes sandbox service."""
    assert classify_process("ChatGPT.exe", r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1\app\ChatGPT.exe", []) == "codex"
    assert classify_process("ChatGPT.exe", r"C:\Users\dev\AppData\Local\ChatGPT\ChatGPT.exe", []) == "codex"
    assert (
        classify_process(
            "extension-host.exe",
            r"C:\Users\dev\.codex\plugins\cache\openai-bundled\chrome\extension-host.exe",
            [],
        )
        == "codex"
    )
    assert (
        classify_process(
            "node.exe",
            r"C:\Program Files\WindowsApps\OpenAI.CodexPrimaryRuntime.v26\dependencies\node\bin\node.exe",
            [],
        )
        == "codex"
    )
    assert (
        classify_process(
            "msedgewebview2.exe",
            r"C:\Program Files (x86)\Microsoft\EdgeWebView\msedgewebview2.exe",
            ["--user-data-dir=C:\\Users\\dev\\.codex\\webview"],
        )
        == "codex"
    )
    assert classify_process("wsl.exe", r"C:\Windows\System32\wsl.exe", ["wsl", "codex", "resume"]) == "codex"
    assert classify_process("Antigravity.exe", r"C:\Users\dev\AppData\Local\Programs\Antigravity\Antigravity.exe", []) == "antigravity"
    assert (
        classify_process(
            "node.exe",
            r"C:\Users\dev\AppData\Roaming\Antigravity\bin\node.exe",
            [],
        )
        == "antigravity"
    )
    # Must NEVER block on always-on sandbox service
    assert classify_process("codex-windows-sandbox-service.exe", r"C:\Program Files\WindowsApps\OpenAI.Codex\codex-windows-sandbox-service.exe", []) is None
    assert classify_process("codex-windows-sandbox-setup.exe", "", []) is None


def test_codex_hydration_golden_schema_and_catalog_sync(tmp_path: Path) -> None:
    """Codex write_chat produces 1:1 native hydration rollout, exact byte offsets, has_user_event=0, and codex.db catalog row."""
    codex_root = tmp_path / ".codex"
    codex_root.mkdir(parents=True)
    _create_codex_catalog_db(codex_root / "sqlite" / "codex.db")

    adapter = CodexAdapter(ToolConfig(enabled=True, path=str(codex_root)))
    proj_dir = tmp_path / "repo"
    proj_dir.mkdir()
    projs = {
        "repo": ProjectItem(
            canonical_key="repo",
            display_path=str(proj_dir),
            name="repo",
            created_at_ms=1_795_370_000_000,
            updated_at_ms=1_795_370_000_000,
        )
    }
    _, projs = adapter.write_projects(projs)

    chat = ChatSession(
        tool="antigravity",
        native_id="11111111-2222-3333-4444-555555555555",
        title="Hydration Golden Verification",
        project_key="repo",
        project_path=str(proj_dir),
        created_at_ms=1_795_370_001_000,
        updated_at_ms=1_795_370_005_000,
        messages=[
            ChatMessage(role="user", text="Check database WAL locking.", timestamp_ms=1_795_370_001_000),
            ChatMessage(role="assistant", text="Added transactional SQLite backup and checkpoint.", timestamp_ms=1_795_370_005_000),
        ],
    ).compute_hashes()

    tid, changed = adapter.write_chat(chat, target_native_id=None, projects_by_key=projs)
    assert changed

    # 1. Verify state_5.sqlite row matches native Codex Desktop expectations
    sconn = sqlite3.connect(str(adapter.state_db))
    sconn.row_factory = sqlite3.Row
    trow = sconn.execute("SELECT * FROM threads WHERE id = ?", (tid,)).fetchone()
    sconn.close()
    assert trow is not None
    assert trow["has_user_event"] == 0
    assert trow["originator"] == "Codex Desktop"
    assert trow["history_mode"] == "paginated"
    assert trow["cli_version"] != "agy-codex-sync"

    # 2. Verify rollout JSONL line-by-line & exact byte offsets in thread_history_1.sqlite
    rp = Path(trow["rollout_path"])
    assert rp.exists()
    assert validate_codex_rollout_file(rp) == []
    raw_bytes = rp.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw_bytes

    lines = [json.loads(ln) for ln in raw_bytes.decode("utf-8").splitlines() if ln.strip()]
    assert lines[0]["type"] == "session_meta"
    assert lines[1]["payload"]["type"] == "task_started"
    assert "model_context_window" in lines[1]["payload"]
    assert lines[2]["type"] == "turn_context"
    assert "permission_profile" in lines[2]["payload"]
    agent_ic = next(
        o for o in lines
        if o["type"] == "event_msg" and (o.get("payload") or {}).get("item", {}).get("type") == "AgentMessage"
    )
    assert agent_ic["payload"]["item"]["phase"] == "final_answer"
    assert agent_ic["payload"]["item"]["content"][0]["type"] == "Text"
    assert lines[-1]["payload"]["type"] == "task_complete"

    hconn = sqlite3.connect(str(adapter.thread_history_db))
    hconn.row_factory = sqlite3.Row
    turn = hconn.execute("SELECT * FROM thread_turns WHERE thread_id = ?", (tid,)).fetchone()
    proj_state = hconn.execute(
        "SELECT * FROM thread_history_projection_state WHERE thread_id = ?", (tid,)
    ).fetchone()
    items = hconn.execute("SELECT * FROM thread_items WHERE thread_id = ? ORDER BY rollout_ordinal", (tid,)).fetchall()
    hconn.close()

    assert turn is not None
    turn_slice = raw_bytes[turn["rollout_byte_offset"] : turn["rollout_end_byte_offset"]].decode("utf-8")
    assert '"task_started"' in turn_slice.splitlines()[0]
    assert '"task_complete"' in turn_slice.splitlines()[-1]
    assert proj_state["next_rollout_byte_offset"] == len(raw_bytes)
    assert proj_state["next_rollout_ordinal"] == len(lines)
    assert len(items) == 2

    # 3. Verify sqlite/codex.db local_thread_catalog was updated and catalog_revision incremented
    cconn = sqlite3.connect(str(adapter.codex_db))
    cconn.row_factory = sqlite3.Row
    cat_row = cconn.execute("SELECT * FROM local_thread_catalog WHERE thread_id = ?", (tid,)).fetchone()
    rev = cconn.execute("SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id = 1").fetchone()[0]
    cconn.close()
    assert cat_row is not None
    assert cat_row["display_title"] == "Hydration Golden Verification"
    assert rev == 3


def test_deep_repair_fixes_spinning_threads_corrupt_codex_db_and_invalid_toml(tmp_path: Path) -> None:
    """Deep Repair fixes legacy spinning threads, recovers corrupt codex.db from db-backups, and restores broken config.toml."""
    codex_root = tmp_path / ".codex"
    codex_root.mkdir(parents=True)
    data_dir = tmp_path / "data"
    cfg = AgentSyncConfig(
        data_dir=str(data_dir),
        max_backups=2,
        tools={"codex": ToolConfig(enabled=True, path=str(codex_root))},
    )
    adapter = CodexAdapter(cfg.tools["codex"])

    # Seed valid config.toml + valid backup FIRST
    adapter.config_toml.write_text('model = "gpt-5"\n', encoding="utf-8")
    _create_codex_catalog_db(adapter.codex_db)
    bmgr = BackupManager(cfg)
    bmgr.create_backup([adapter], reason="healthy-baseline")

    # Seed a valid auto-recovery snapshot in .codex/sqlite/db-backups/sqlite-99999-0/codex.db
    rec_dir = adapter.sqlite_backups_dir / "sqlite-99999-0"
    rec_dir.mkdir(parents=True)
    sqlite_backup_file(adapter.codex_db, rec_dir / "codex.db")

    # Now simulate ALL THREE real-world failures at once!
    # (a) Corrupt config.toml
    adapter.config_toml.write_text("model = [BROKEN TOML\n", encoding="utf-8")
    # (b) Corrupt codex.db with garbage bytes
    adapter.codex_db.write_bytes(b"NOT A SQLITE DATABASE" * 50)
    # (c) Legacy spinning rollout + broken state_5.sqlite row (cli_version='agy-codex-sync', has_user_event=1)
    bad_tid = "ae2acd5a-5020-4e47-96ac-afb3eabace06"
    bad_rollout = adapter.sessions_dir / "2026" / "11" / "22" / f"rollout-2026-11-22T16-58-33-{bad_tid}.jsonl"
    bad_rollout.parent.mkdir(parents=True, exist_ok=True)
    bad_rollout.write_text(
        json.dumps({"timestamp": "2026-11-22T16:58:33.000Z", "type": "session_meta", "payload": {"id": bad_tid, "timestamp": "2026-11-22T16:58:33.000Z", "cwd": str(tmp_path), "originator": "agy-codex-sync", "cli_version": "agy-codex-sync", "source": "vscode"}}) + "\n"
        + json.dumps({"timestamp": "2026-11-22T16:58:34.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hello from Antigravity"}]}}) + "\n",
        encoding="utf-8",
    )
    sconn = adapter._ensure_state_db()
    sconn.execute(
        """
        INSERT INTO threads(id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
            sandbox_policy, approval_mode, has_user_event, cli_version, originator, first_user_message)
        VALUES(?, ?, 1795366713, 1795366714, 'vscode', 'openai', ?, 'Spinning Thread',
            '{"type":"disabled"}', 'never', 1, 'agy-codex-sync', 'agy-codex-sync', 'Hello from Antigravity')
        """,
        (bad_tid, str(bad_rollout), str(tmp_path)),
    )
    sconn.commit()
    sconn.close()

    doc = DoctorEngine(cfg, [adapter])
    issues = doc.diagnose()
    codes = {i.code for i in issues}
    assert "CODEX_INVALID_TOML" in codes
    assert "CODEX_SQLITE_CORRUPT" in codes
    assert "CODEX_BROKEN_ROLLOUT" in codes
    assert "CODEX_SPINNING_THREAD" in codes
    assert "CODEX_DB_RECOVERY_BACKUPS" in codes

    # Execute Deep Repair!
    results, _ = doc.repair(dry_run=False, force=True, check_processes=False)
    assert sum(r.fixed_count for r in results) >= 3

    # Verify ALL THREE were completely healed
    assert 'model = "gpt-5"' in adapter.config_toml.read_text(encoding="utf-8")
    ok_cdb, _ = sqlite_integrity_check(adapter.codex_db, readonly=True)
    assert ok_cdb
    assert validate_codex_rollout_file(bad_rollout) == []

    # Post-repair doctor should have ZERO errors (only the informational db-backups notice)
    remaining_errors = [i for i in doc.diagnose() if i.severity == "error"]
    assert remaining_errors == []


def test_backup_retention_and_integrity_verification(tmp_path: Path) -> None:
    """BackupManager enforces retention count and detects tampered payload SHA-256 cleanly."""
    codex_root = tmp_path / ".codex"
    codex_root.mkdir(parents=True)
    adapter = CodexAdapter(ToolConfig(enabled=True, path=str(codex_root)))
    adapter.config_toml.write_text('model = "gpt-5"\n', encoding="utf-8")

    cfg = AgentSyncConfig(
        data_dir=str(tmp_path / "data"),
        max_backups=2,
        tools={"codex": ToolConfig(enabled=True, path=str(codex_root))},
    )
    bmgr = BackupManager(cfg)
    bmgr.create_backup([adapter], reason="b1")
    bmgr.create_backup([adapter], reason="b2")
    bmgr.create_backup([adapter], reason="b3")

    listed = bmgr.list_backups()
    assert len(listed) == 2  # retention pruned oldest backup

    reports = bmgr.verify_all_backups()
    assert len(reports) == 2
    assert all(r.ok for r in reports)

    # Tamper with one backed-up file and confirm verify_backup catches it
    first_b = listed[0]
    tampered = cfg.backups_dir / first_b.backup_id / first_b.entries[0].backup_rel_path
    tampered.write_bytes(b"TAMPERED")
    bad_rep = bmgr.verify_backup(first_b.backup_id)
    assert not bad_rep.ok
    assert any("SHA-256 mismatch" in e for e in bad_rep.errors)
