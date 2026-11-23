"""Regression tests for v0.5.1: Codex project binding, URI/Cyrillic canonicalization, and Antigravity bidirectional chat sync."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.codex import CodexAdapter
from agentsync.config import AgentSyncConfig, SyncOptions, ToolConfig
from agentsync.engine.sync import SyncEngine
from agentsync.models.canonical import ChatMessage, ChatSession, ProjectItem
from agentsync.utils.paths import (
    canonical_windows_cwd,
    file_uri_to_path,
    normalize_path,
    path_dedup_key,
    path_to_file_uri,
    to_native_path,
)
from agentsync.utils.proto_wire import (
    decode_trajectory_metadata,
    first_bytes,
    parse_wire,
    read_agyhub_summaries_pb,
)


def test_uri_and_cyrillic_path_canonicalization() -> None:
    """All URI, percent-encoded Cyrillic, WSL mount, leading-slash, and extended-prefix variants must canonicalize identically."""
    variants = [
        "file:///c%3A/Users/method/Documents/%D0%A1%D0%B8%D0%BD%D1%85%D1%80%D0%BE%D0%BD%D0%BD%D0%B8%D0%BD%D0%B3",
        "file:///c:/Users/method/Documents/Синхроннинг/",
        "file:///C:/Users/method/Documents/%D0%A1%D0%B8%D0%BD%D1%85%D1%80%D0%BE%D0%BD%D0%BD%D0%B8%D0%BD%D0%B3/",
        r"\\?\c:\Users\method\Documents\Синхроннинг\\",
        "/C:/Users/method/Documents/Синхроннинг",
        "win32:/mnt/c/Users/method/Documents/Синхроннинг",
        "file/C%3A%5CUsers%5Cmethod%5CDocuments%5C%D0%A1%D0%B8%D0%BD%D1%85%D1%80%D0%BE%D0%BD%D0%BD%D0%B8%D0%BD%D0%B3",
    ]
    expected_key = "c:/users/method/documents/синхроннинг"
    expected_cwd = r"C:\Users\method\Documents\Синхроннинг"

    for raw in variants:
        assert path_dedup_key(raw) == expected_key, f"Failed dedup key for {raw!r}"
        assert canonical_windows_cwd(raw) == expected_cwd, f"Failed canonical cwd for {raw!r}"
        assert to_native_path(raw) == expected_cwd, f"Failed native path for {raw!r}"
        assert normalize_path(raw) == "C:/Users/method/Documents/Синхроннинг"

    # Also test paths with spaces (%20)
    space_uri = "file:///c%3A/Users/method/Documents/My%20Project%20%D0%A2%D0%B5%D1%81%D1%82/"
    assert file_uri_to_path(space_uri) == "c:/Users/method/Documents/My Project Тест/"
    assert canonical_windows_cwd(space_uri) == r"C:\Users\method\Documents\My Project Тест"


def test_golden_antigravity_to_codex_project_binding_and_fallback(tmp_path: Path) -> None:
    """Chats synced from Antigravity -> Codex must bind across rollout, state_5, codex.db, and unified-sidebar-project-order-v1."""
    codex_dir = tmp_path / ".codex"
    gemini_dir = tmp_path / ".gemini"
    appdata_agy = tmp_path / "AppData" / "Roaming" / "Antigravity"
    state_dir = tmp_path / "state"
    ws_dir = tmp_path / "workspaces" / "Синхроннинг Проект"
    ws_dir.mkdir(parents=True)

    # Pre-seed Codex with codex.db (local_thread_catalog + metadata) and .codex-global-state.json with unified-sidebar-project-order-v1
    (codex_dir / "sqlite").mkdir(parents=True)
    cdb = sqlite3.connect(str(codex_dir / "sqlite" / "codex.db"))
    cdb.executescript(
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
            catalog_revision INTEGER NOT NULL
        );
        INSERT INTO local_thread_catalog_metadata(id, catalog_revision) VALUES(1, 10);
        """
    )
    cdb.commit()
    cdb.close()

    (codex_dir / ".codex-global-state.json").write_text(
        json.dumps(
            {
                "local-projects": {},
                "project-order": [],
                "electron-persisted-atom-state": {
                    "unified-sidebar-project-order-v1": []
                },
            }
        ),
        encoding="utf-8",
    )

    codex = CodexAdapter(ToolConfig(enabled=True, path=str(codex_dir)))
    agy = AntigravityAdapter(
        ToolConfig(enabled=True, path=str(gemini_dir), extra_paths={"appdata": str(appdata_agy)})
    )

    # Seed project and chat in Antigravity
    pkey = path_dedup_key(str(ws_dir))
    proj = ProjectItem(
        canonical_key=pkey,
        display_path=normalize_path(str(ws_dir)),
        name="Синхроннинг Проект",
        created_at_ms=1795000000000,
        updated_at_ms=1795000000000,
    )
    _, agy_projs = agy.write_projects({pkey: proj})

    agy_chat = ChatSession(
        tool="antigravity",
        native_id="39502221-070f-483b-bf39-9cbf17c1c859",
        title="Cyrillic Workspace Bound Chat",
        project_key=pkey,
        project_path=proj.display_path,
        created_at_ms=1795000010000,
        updated_at_ms=1795000020000,
        messages=[
            ChatMessage(role="user", text="Check project binding in Cyrillic folder", timestamp_ms=1795000010000),
            ChatMessage(role="assistant", text="Verified project binding!", timestamp_ms=1795000020000),
        ],
    ).compute_hashes()
    agy.write_chat(agy_chat, target_native_id=agy_chat.native_id, projects_by_key=agy_projs)

    # Test fallback extraction: even if workspace_uris column is cleared in conversation_summaries.db,
    # AntigravityAdapter.read_chats() must recover project_key from raw_summary / trajectory_metadata_blob!
    scon = sqlite3.connect(str(agy.summaries_db))
    scon.execute("UPDATE conversation_summaries SET workspace_uris = '' WHERE conversation_id = ?", (agy_chat.native_id,))
    scon.commit()
    scon.close()

    read_back = agy.read_chats({}, include_subagents=False)
    assert len(read_back) == 1
    assert read_back[0].project_key == pkey

    # Now run full bidirectional SyncEngine (Antigravity -> Codex)
    cfg = AgentSyncConfig(
        state_dir=state_dir,
        backups_dir=tmp_path / "backups",
        quarantine_dir=tmp_path / "quarantine",
        conflicts_dir=tmp_path / "conflicts",
        tools={
            "codex": codex.tool_config,
            "antigravity": agy.tool_config,
        },
    )
    engine = SyncEngine(cfg, [codex, agy], SyncOptions(force=True, no_backup=True))
    plan1 = engine.run(check_processes=False, create_backup=False)
    assert any(a.category == "chats" and a.target_tool == "codex" for a in plan1.mutating_actions)

    # Verify all 5 Codex project-binding stores
    c_projs = codex.read_projects()
    assert pkey in c_projs
    codex_pid = c_projs[pkey].primary_id("codex")
    codex_lid = c_projs[pkey].secondary_id("codex")
    expected_cwd = to_native_path(str(ws_dir))
    assert codex_pid and codex_lid

    # 1. state_5.sqlite
    s5 = sqlite3.connect(str(codex.state_db))
    t_row = s5.execute("SELECT id, cwd, project_id, rollout_path FROM threads").fetchone()
    p_root = s5.execute("SELECT path FROM project_roots WHERE project_id = ?", (codex_pid,)).fetchone()
    s5.close()
    assert t_row is not None
    tid = t_row[0]
    assert t_row[1] == expected_cwd
    assert not t_row[1].startswith("\\\\?\\")
    assert t_row[2] == codex_pid
    assert p_root[0] == expected_cwd

    # 2. rollout-*.jsonl
    rollout_lines = Path(t_row[3]).read_text(encoding="utf-8").splitlines()
    meta_pl = json.loads(rollout_lines[0])["payload"]
    assert meta_pl["cwd"] == expected_cwd
    assert meta_pl["runtime_workspace_roots"] == [expected_cwd]
    turn_ctx_lines = [json.loads(ln)["payload"] for ln in rollout_lines if json.loads(ln).get("type") == "turn_context"]
    assert turn_ctx_lines and turn_ctx_lines[0]["cwd"] == expected_cwd

    # 3. sqlite/codex.db local_thread_catalog
    cdb = sqlite3.connect(str(codex.codex_db))
    cat_row = cdb.execute(
        "SELECT cwd, project_id, thread_source FROM local_thread_catalog WHERE host_id='local' AND thread_id=?",
        (tid,),
    ).fetchone()
    rev_row = cdb.execute("SELECT catalog_revision FROM local_thread_catalog_metadata WHERE id=1").fetchone()
    cdb.close()
    assert cat_row == (expected_cwd, codex_pid, "user")
    assert rev_row[0] > 10

    # 4. .codex-global-state.json (including unified-sidebar-project-order-v1)
    gs = json.loads(codex.global_state_json.read_text(encoding="utf-8"))
    assert gs["thread-project-assignments"][tid] == {"projectKind": "local", "projectId": codex_lid}
    assert gs["thread-project-membership-host-ids"][tid] == "local"
    assert tid not in (gs.get("projectless-thread-ids") or [])
    unified_sidebar = gs["electron-persisted-atom-state"]["unified-sidebar-project-order-v1"]
    assert f"codex:project:{codex_lid}" in unified_sidebar
    assert gs["electron-persisted-atom-state"][f"sidebar-project-expanded-v1-codex:{codex_lid}"] is True


def test_repair_rebinds_unbound_codex_threads_and_malformed_agy_summaries(tmp_path: Path) -> None:
    """doctor + repair must detect and heal CODEX_UNBOUND_SYNCED_THREAD and AGY_UNBOUND_OR_MALFORMED_SUMMARY in place."""
    codex_dir = tmp_path / ".codex"
    gemini_dir = tmp_path / ".gemini"
    appdata_agy = tmp_path / "AppData" / "Roaming" / "Antigravity"
    ws_dir = tmp_path / "workspaces" / "bugs"
    ws_dir.mkdir(parents=True)

    (codex_dir / "sqlite").mkdir(parents=True)
    cdb = sqlite3.connect(str(codex_dir / "sqlite" / "codex.db"))
    cdb.executescript(
        """
        CREATE TABLE local_thread_catalog (
            host_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            display_title TEXT NOT NULL,
            source_created_at REAL NOT NULL,
            source_updated_at REAL NOT NULL,
            cwd TEXT,
            source_kind TEXT NOT NULL,
            model_provider TEXT,
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
            catalog_revision INTEGER NOT NULL
        );
        INSERT INTO local_thread_catalog_metadata(id, catalog_revision) VALUES(1, 1);
        """
    )
    cdb.commit()
    cdb.close()

    codex = CodexAdapter(ToolConfig(enabled=True, path=str(codex_dir)))
    agy = AntigravityAdapter(
        ToolConfig(enabled=True, path=str(gemini_dir), extra_paths={"appdata": str(appdata_agy)})
    )

    pkey = path_dedup_key(str(ws_dir))
    proj = ProjectItem(
        canonical_key=pkey,
        display_path=normalize_path(str(ws_dir)),
        name="VPN",
        created_at_ms=1795000000000,
        updated_at_ms=1795000000000,
    )
    _, c_projs = codex.write_projects({pkey: proj})
    codex_pid = c_projs[pkey].primary_id("codex")
    codex_lid = c_projs[pkey].secondary_id("codex")

    tid = "ef0d580d-735c-47f3-8b19-faf2ba15c12b"
    chat = ChatSession(
        tool="codex",
        native_id=tid,
        title="Unbound VPN Thread",
        project_key=pkey,
        project_path=proj.display_path,
        created_at_ms=1795000010000,
        updated_at_ms=1795000020000,
        messages=[
            ChatMessage(role="user", text="Fix VPN subscriptions", timestamp_ms=1795000010000),
            ChatMessage(role="assistant", text="Done fixing subscriptions.", timestamp_ms=1795000020000),
        ],
    ).compute_hashes()
    codex.write_chat(chat, target_native_id=tid, projects_by_key=c_projs)

    # Simulate the real-world corruption observed on Windows 11:
    # 1. state_5.sqlite has \\?\ prefix on cwd and project_id = NULL
    # 2. codex.db local_thread_catalog has project_id = NULL and thread_source = NULL
    # 3. .codex-global-state.json is missing thread-project-assignments[tid] and unified-sidebar-project-order-v1 is empty
    s5 = sqlite3.connect(str(codex.state_db))
    s5.execute(
        "UPDATE threads SET cwd = ?, project_id = NULL WHERE id = ?",
        ("\\\\?\\" + to_native_path(str(ws_dir)), tid),
    )
    s5.commit()
    s5.close()

    cdb = sqlite3.connect(str(codex.codex_db))
    cdb.execute("UPDATE local_thread_catalog SET project_id = NULL, thread_source = NULL WHERE thread_id = ?", (tid,))
    cdb.commit()
    cdb.close()

    gs = json.loads(codex.global_state_json.read_text(encoding="utf-8"))
    gs["thread-project-assignments"] = {}
    gs["projectless-thread-ids"] = [tid]
    gs["electron-persisted-atom-state"] = {"unified-sidebar-project-order-v1": []}
    codex.global_state_json.write_text(json.dumps(gs), encoding="utf-8")

    # Run Codex doctor -> must report CODEX_UNBOUND_SYNCED_THREAD
    c_issues = codex.doctor()
    c_codes = {i.code for i in c_issues}
    assert "CODEX_UNBOUND_SYNCED_THREAD" in c_codes

    # Run Codex repair -> must re-bind in place without creating duplicates
    c_rep = codex.repair(tmp_path / "quarantine", dry_run=False)
    assert c_rep.fixed_count >= 1
    assert "CODEX_UNBOUND_SYNCED_THREAD" not in {i.code for i in codex.doctor()}

    s5 = sqlite3.connect(str(codex.state_db))
    all_threads = s5.execute("SELECT id, cwd, project_id FROM threads").fetchall()
    s5.close()
    assert len(all_threads) == 1
    assert all_threads[0] == (tid, to_native_path(str(ws_dir)), codex_pid)

    cdb = sqlite3.connect(str(codex.codex_db))
    cat_after = cdb.execute(
        "SELECT cwd, project_id, thread_source FROM local_thread_catalog WHERE thread_id = ?",
        (tid,),
    ).fetchone()
    cdb.close()
    assert cat_after == (to_native_path(str(ws_dir)), codex_pid, "user")

    gs_after = json.loads(codex.global_state_json.read_text(encoding="utf-8"))
    assert gs_after["thread-project-assignments"][tid] == {"projectKind": "local", "projectId": codex_lid}
    assert tid not in gs_after["projectless-thread-ids"]
    assert f"codex:project:{codex_lid}" in gs_after["electron-persisted-atom-state"]["unified-sidebar-project-order-v1"]

    # Now test Antigravity doctor + repair on a chat with empty project_id = "" (invisible in Antigravity UI!)
    acid = "5359ee8b-8921-40f8-9d73-7091bdfe4b4e"
    unbound_chat = ChatSession(
        tool="antigravity",
        native_id=acid,
        title="General Playground Chat",
        project_key=None,
        created_at_ms=1795347875000,
        updated_at_ms=1795358082000,
        messages=[
            ChatMessage(role="user", text="Remove all skills", timestamp_ms=1795347875000),
            ChatMessage(role="assistant", text="Checking installed skills...", timestamp_ms=1795347877000),
        ],
    ).compute_hashes()
    agy.write_chat(unbound_chat, target_native_id=acid, projects_by_key={})

    # Corrupt conversation_summaries.db to project_id = "" and strip field 18 from trajectory_metadata_blob
    scon = sqlite3.connect(str(agy.summaries_db))
    scon.execute("UPDATE conversation_summaries SET project_id = '' WHERE conversation_id = ?", (acid,))
    scon.commit()
    scon.close()

    a_issues = agy.doctor()
    assert "AGY_UNBOUND_OR_MALFORMED_SUMMARY" in {i.code for i in a_issues}

    a_rep = agy.repair(tmp_path / "quarantine", dry_run=False)
    assert a_rep.fixed_count >= 1
    assert "AGY_UNBOUND_OR_MALFORMED_SUMMARY" not in {i.code for i in agy.doctor()}

    scon = sqlite3.connect(str(agy.summaries_db))
    fixed_row = scon.execute(
        "SELECT project_id, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
        (acid,),
    ).fetchone()
    scon.close()
    assert fixed_row[0] == "outside-of-project"
    raw_fields = parse_wire(fixed_row[1])
    assert 28 in {fn for fn, _, _ in raw_fields}
    f17 = first_bytes(raw_fields, 17)
    assert f17 is not None
    assert decode_trajectory_metadata(f17)["project_id"] == "outside-of-project"


def test_codex_to_antigravity_chat_sync_and_three_run_idempotency(tmp_path: Path) -> None:
    """Codex -> Antigravity must write 1:1 native Antigravity structure (including field 18 for outside-of-project) and be 3-run idempotent."""
    codex_dir = tmp_path / ".codex"
    gemini_dir = tmp_path / ".gemini"
    appdata_agy = tmp_path / "AppData" / "Roaming" / "Antigravity"
    state_dir = tmp_path / "state"
    ws_dir = tmp_path / "workspaces" / "Проект"
    ws_dir.mkdir(parents=True)

    codex = CodexAdapter(ToolConfig(enabled=True, path=str(codex_dir)))
    agy = AntigravityAdapter(
        ToolConfig(enabled=True, path=str(gemini_dir), extra_paths={"appdata": str(appdata_agy)})
    )

    pkey = path_dedup_key(str(ws_dir))
    proj = ProjectItem(
        canonical_key=pkey,
        display_path=normalize_path(str(ws_dir)),
        name="Документ",
        created_at_ms=1795350389000,
        updated_at_ms=1795350389000,
    )
    _, c_projs = codex.write_projects({pkey: proj})

    # 1. Project-bound Codex chat
    c_bound = ChatSession(
        tool="codex",
        native_id="01a1ee31-d5df-7d43-8148-5ca2ab66ff2c",
        title="Bound Codex Chat",
        project_key=pkey,
        project_path=proj.display_path,
        created_at_ms=1795350400000,
        updated_at_ms=1795350500000,
        messages=[
            ChatMessage(role="user", text="Redesign VPN account cabinet", timestamp_ms=1795350400000),
            ChatMessage(role="assistant", text="Starting account redesign.", timestamp_ms=1795350500000),
        ],
    ).compute_hashes()
    codex.write_chat(c_bound, target_native_id=c_bound.native_id, projects_by_key=c_projs)

    # 2. General (projectless) Codex chat
    c_general = ChatSession(
        tool="codex",
        native_id="01a20312-1f66-7873-b109-8058deaaa782",
        title="Prompt Studio 3D Card",
        project_key=None,
        created_at_ms=1795351482000,
        updated_at_ms=1795358082000,
        messages=[
            ChatMessage(role="user", text="Build 3D developer business card", timestamp_ms=1795351482000),
            ChatMessage(role="assistant", text="Here is the 3D scene setup.", timestamp_ms=1795358082000),
        ],
    ).compute_hashes()
    codex.write_chat(c_general, target_native_id=c_general.native_id, projects_by_key=c_projs)

    cfg = AgentSyncConfig(
        state_dir=state_dir,
        backups_dir=tmp_path / "backups",
        quarantine_dir=tmp_path / "quarantine",
        conflicts_dir=tmp_path / "conflicts",
        tools={
            "codex": codex.tool_config,
            "antigravity": agy.tool_config,
        },
    )
    engine = SyncEngine(cfg, [codex, agy], SyncOptions(force=True, no_backup=True))

    # Run 1: Sync Codex -> Antigravity
    plan1 = engine.run(check_processes=False, create_backup=False)
    agy_chat_creates = [
        a for a in plan1.mutating_actions if a.category == "chats" and a.target_tool == "antigravity"
    ]
    assert len(agy_chat_creates) == 2

    # Inspect Antigravity stores for both chats
    a_projs = agy.read_projects()
    assert pkey in a_projs
    agy_pid = a_projs[pkey].primary_id("antigravity")
    assert agy_pid

    a_chats = {c.title: c for c in agy.read_chats(a_projs)}
    assert set(a_chats.keys()) == {"Bound Codex Chat", "Prompt Studio 3D Card"}
    assert a_chats["Bound Codex Chat"].project_key == pkey
    assert a_chats["Prompt Studio 3D Card"].project_key is None
    assert not a_chats["Bound Codex Chat"].needs_repair
    assert not a_chats["Prompt Studio 3D Card"].needs_repair

    pb_map = read_agyhub_summaries_pb(agy.hub_summaries_pb)
    scon = sqlite3.connect(str(agy.summaries_db))
    scon.row_factory = sqlite3.Row
    for title, expected_pid, expected_ws_name in [
        ("Bound Codex Chat", agy_pid, "Проект"),
        ("Prompt Studio 3D Card", "outside-of-project", ""),
    ]:
        cid = a_chats[title].native_id
        assert cid in pb_map
        assert (agy.annotations_dir / f"{cid}.pbtxt").exists()
        assert (agy.brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl").exists()

        srow = scon.execute(
            "SELECT project_id, workspace_uris, raw_summary FROM conversation_summaries WHERE conversation_id = ?",
            (cid,),
        ).fetchone()
        assert srow["project_id"] == expected_pid
        if expected_pid == "outside-of-project":
            assert srow["workspace_uris"] == ""
        else:
            assert srow["workspace_uris"] == json.dumps(
                [path_to_file_uri(str(ws_dir), encode_drive_colon=True)], separators=(",", ":")
            )

        # Verify wire field 18 in raw_summary.trajectory_metadata_blob AND conversations/<cid>.db
        rfields = parse_wire(srow["raw_summary"])
        assert 28 in {fn for fn, _, _ in rfields}
        f17 = first_bytes(rfields, 17)
        assert f17 is not None
        assert decode_trajectory_metadata(f17)["project_id"] == expected_pid

        fts_row = scon.execute(
            "SELECT workspace, project FROM conversations_fts WHERE conversation_id = ? AND chunk_index = -1",
            (cid,),
        ).fetchone()
        assert fts_row["workspace"] == expected_ws_name

        c_db = sqlite3.connect(str(agy.conversations_dir / f"{cid}.db"))
        assert c_db.execute("PRAGMA user_version").fetchone()[0] == 1
        tblob = c_db.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()[0]
        c_db.close()
        assert decode_trajectory_metadata(tblob)["project_id"] == expected_pid
    scon.close()

    # Run 2 & Run 3: Strict 0-diff idempotency
    plan2 = engine.run(check_processes=False, create_backup=False)
    assert len(plan2.mutating_actions) == 0, f"Expected 0 mutations on run 2, got: {plan2.mutating_actions}"

    plan3 = engine.run(check_processes=False, create_backup=False)
    assert len(plan3.mutating_actions) == 0, f"Expected 0 mutations on run 3, got: {plan3.mutating_actions}"
    assert len(codex.read_chats(c_projs)) == 2
    assert len(agy.read_chats(a_projs)) == 2

    # Simulate Codex losing project binding on Bound Codex Chat while message text is unchanged:
    # SyncEngine must detect the binding mismatch/repair need and re-bind Codex in place (UPDATE, not duplicate!)
    s5 = sqlite3.connect(str(codex.state_db))
    s5.execute("UPDATE threads SET project_id = NULL, cwd = ? WHERE id = ?", (str(codex_dir / "synced-chats"), c_bound.native_id))
    s5.commit()
    s5.close()
    gs = json.loads(codex.global_state_json.read_text(encoding="utf-8"))
    gs["thread-project-assignments"] = {}
    gs["projectless-thread-ids"] = [c_bound.native_id, c_general.native_id]
    codex.global_state_json.write_text(json.dumps(gs), encoding="utf-8")

    plan4 = engine.run(check_processes=False, create_backup=False)
    codex_updates = [a for a in plan4.mutating_actions if a.category == "chats" and a.target_tool == "codex"]
    assert len(codex_updates) == 1
    assert codex_updates[0].action == "UPDATE"
    assert len(codex.read_chats(c_projs)) == 2

    # Next run is 0-diff idempotent again
    plan5 = engine.run(check_processes=False, create_backup=False)
    assert len(plan5.mutating_actions) == 0
