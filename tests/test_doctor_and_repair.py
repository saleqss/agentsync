"""Tests for doctor diagnostics and quarantine/repair across Codex, Antigravity, and OpenCode."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.codex import CodexAdapter, validate_codex_rollout_file
from agentsync.adapters.opencode import OpenCodeAdapter
from agentsync.config import AgentSyncConfig
from agentsync.engine.doctor import DoctorEngine
from agentsync.utils.proto_wire import (
    RUN_STATUS_IDLE,
    RUN_STATUS_RUNNING,
    encode_raw_summary,
    first_varint,
    parse_wire,
    read_agyhub_summaries_pb,
)


def test_doctor_detects_and_repairs_broken_codex_rollout_and_duplicates(sandbox: dict[str, Any]) -> None:
    """Broken v0.1 Codex rollout (missing id/timestamp/cli_version/source) is detected, quarantined, and regenerated cleanly."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex: CodexAdapter = sandbox["codex"]

    # Corrupt cx-chat-unique's rollout to reproduce the exact v0.1 resume bug
    conn = sqlite3.connect(str(codex.state_db))
    rp_str = conn.execute("SELECT rollout_path FROM threads WHERE id = 'cx-chat-unique'").fetchone()[0]
    conn.close()
    broken_path = Path(rp_str)

    # Write broken v0.1 minimal session_meta WITH UTF-8 BOM
    broken_lines = [
        json.dumps(
            {
                "timestamp": "2026-11-22T16:58:33.000Z",
                "type": "session_meta",
                "payload": {
                    "session_id": "cx-chat-unique",
                    "cwd": str(sandbox["workspaces"]["app"]),
                    "originator": "agy-codex-sync",
                },
            }
        ),
        json.dumps(
            {
                "timestamp": "2026-11-22T16:58:34.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "How do we fix routing latency?"}],
                },
            }
        ),
    ]
    broken_path.write_bytes(b"\xef\xbb\xbf" + ("\n".join(broken_lines) + "\n").encode("utf-8"))

    # 1. Doctor must flag both CODEX_BROKEN_ROLLOUT and CODEX_DUPLICATE_PROJECT
    doc = DoctorEngine(cfg, [codex])
    issues = doc.diagnose()
    codes = {i.code for i in issues}
    assert "CODEX_BROKEN_ROLLOUT" in codes
    assert "CODEX_DUPLICATE_PROJECT" in codes
    assert all(i.fixable for i in issues if i.code in {"CODEX_BROKEN_ROLLOUT", "CODEX_DUPLICATE_PROJECT"})

    # 2. Repair dry-run must NOT modify the broken rollout
    dry_results, dry_bid = doc.repair(dry_run=True, check_processes=False)
    assert dry_bid is None
    assert len(dry_results) == 1
    assert broken_path.read_bytes().startswith(b"\xef\xbb\xbf")

    # 3. Live repair must back up, quarantine the broken file, regenerate a conformant rollout, and collapse duplicates
    results, backup_id = doc.repair(dry_run=False, check_processes=False)
    assert backup_id is not None
    assert len(results) == 1
    assert len(results[0].quarantined_files) == 1
    assert Path(results[0].quarantined_files[0]).exists()

    # Rollout now passes strict native validation (no BOM, all required session_meta keys)
    assert validate_codex_rollout_file(broken_path) == []

    # Follow-up doctor check must report 0 issues!
    assert doc.diagnose() == []


def test_doctor_detects_and_repairs_antigravity_hub_pb_user_version_and_status(sandbox: dict[str, Any]) -> None:
    """Doctor detects missing agyhub_summaries_proto.pb entries, user_version=0, and field 5=2, and repairs all 3."""
    cfg: AgentSyncConfig = sandbox["config"]
    agy: AntigravityAdapter = sandbox["antigravity"]

    # Reproduce all 3 exact Antigravity v0.1 bugs on 'agy-chat-unique':
    # Bug A: delete agyhub_summaries_proto.pb so conversations are invisible in Hub UI
    agy.hub_summaries_pb.unlink(missing_ok=True)

    # Bug B: downgrade conversations/agy-chat-unique.db PRAGMA user_version back to 0
    cdb = agy.conversations_dir / "agy-chat-unique.db"
    cc = sqlite3.connect(str(cdb))
    cc.execute("PRAGMA user_version = 0")
    cc.commit()
    cc.close()

    # Bug C: overwrite raw_summary with wrong field 5 = 2 (RUNNING) on an IDLE row
    bad_raw = encode_raw_summary(
        title="Antigravity Exclusive Chat",
        step_count=2,
        created_at_ms=1_795_200_000_000,
        last_modified_ms=1_795_200_010_000,
        last_user_input_ms=1_795_200_000_000,
        trajectory_id="traj-bad",
        cascade_id="agy-chat-unique",
        workspace_uri="",
        project_id="outside-of-project",
        run_status=RUN_STATUS_RUNNING,
    )
    sc = sqlite3.connect(str(agy.summaries_db))
    sc.execute(
        "UPDATE conversation_summaries SET raw_summary = ? WHERE conversation_id = 'agy-chat-unique'",
        (bad_raw,),
    )
    sc.commit()
    sc.close()

    doc = DoctorEngine(cfg, [agy])
    issues = doc.diagnose()
    codes = {i.code for i in issues}
    assert "AGY_MISSING_HUB_SUMMARY_PB" in codes
    assert "AGY_RAW_SUMMARY_STATUS_MISMATCH" in codes
    assert "AGY_CONV_DB_USER_VERSION" in codes

    # Run live repair
    results, backup_id = doc.repair(dry_run=False, check_processes=False)
    assert backup_id is not None
    assert results[0].fixed_count >= 2

    # Verify all 3 bugs are fixed on disk
    pb_map = read_agyhub_summaries_pb(agy.hub_summaries_pb)
    assert {"agy-chat-unique", "agy-chat-dup"}.issubset(set(pb_map.keys()))

    cc2 = sqlite3.connect(str(cdb))
    assert cc2.execute("PRAGMA user_version").fetchone()[0] == 1
    cc2.close()

    sc2 = sqlite3.connect(str(agy.summaries_db))
    fixed_raw = sc2.execute(
        "SELECT raw_summary FROM conversation_summaries WHERE conversation_id = 'agy-chat-unique'"
    ).fetchone()[0]
    sc2.close()
    assert first_varint(parse_wire(fixed_raw), 5) == RUN_STATUS_IDLE
    assert pb_map["agy-chat-unique"] == fixed_raw

    # Follow-up doctor check must report 0 issues!
    assert doc.diagnose() == []


def test_doctor_and_repair_opencode_bom(sandbox: dict[str, Any]) -> None:
    """Doctor flags UTF-8 BOM in opencode.json and repair strips it atomically."""
    cfg: AgentSyncConfig = sandbox["config"]
    oc: OpenCodeAdapter = sandbox["opencode"]
    oc.opencode_json.write_bytes(b'\xef\xbb\xbf{"mcp": {"servers": {}}}\n')

    doc = DoctorEngine(cfg, [oc])
    assert any(i.code == "OPENCODE_JSON_BOM" for i in doc.diagnose())

    results, _ = doc.repair(dry_run=False, check_processes=False)
    assert results[0].fixed_count == 1
    assert not oc.opencode_json.read_bytes().startswith(b"\xef\xbb\xbf")
    assert doc.diagnose() == []
