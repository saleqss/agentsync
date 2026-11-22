"""Golden-file schema conformance tests for Codex rollout JSONL and Antigravity hub stores."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.codex import (
    REQUIRED_SESSION_META_KEYS,
    CodexAdapter,
    validate_codex_rollout_file,
)
from agentsync.models.canonical import ChatMessage, ChatSession
from agentsync.utils.proto_wire import (
    RUN_STATUS_IDLE,
    decode_step_text,
    decode_trajectory_metadata,
    first_varint,
    parse_wire,
    read_agyhub_summaries_pb,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_golden_native_codex_fixture_passes_validator() -> None:
    golden = FIXTURES_DIR / "codex_native_rollout.jsonl"
    assert validate_codex_rollout_file(golden) == []


def test_generated_codex_rollout_conforms_strictly_to_native_schema(sandbox: dict[str, object]) -> None:
    codex: CodexAdapter = sandbox["codex"]  # type: ignore[assignment]
    golden_lines = [
        json.loads(line)
        for line in (FIXTURES_DIR / "codex_native_rollout.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    golden_meta_keys = set(golden_lines[0]["payload"].keys())

    projs = codex.read_projects()
    app_key = next(k for k in projs if k.endswith("/core-app"))

    chat = ChatSession(
        tool="antigravity",
        native_id="agy-origin-001",
        title="Conformant Rollout Verification",
        project_key=app_key,
        created_at_ms=1_795_300_000_123,
        updated_at_ms=1_795_300_045_456,
        messages=[
            ChatMessage(role="user", text="First user message", timestamp_ms=1_795_300_000_123),
            ChatMessage(role="assistant", text="First assistant answer", timestamp_ms=1_795_300_010_000),
            ChatMessage(role="user", text="Second follow-up", timestamp_ms=1_795_300_030_000),
            ChatMessage(role="assistant", text="Second complete answer", timestamp_ms=1_795_300_045_456),
        ],
    ).compute_hashes()

    tid, _ = codex.write_chat(chat, target_native_id=None, projects_by_key=projs)

    # Fetch rollout path from state_5.sqlite
    conn = sqlite3.connect(str(codex.state_db))
    row = conn.execute("SELECT rollout_path, cli_version, history_mode FROM threads WHERE id = ?", (tid,)).fetchone()
    conn.close()
    assert row is not None
    rollout_path = Path(row[0])
    assert rollout_path.exists()
    assert row[2] == "paginated"

    # 1. Strict byte-level checks: UTF-8 without BOM, LF only (no CRLF)
    raw_bytes = rollout_path.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf"), "Rollout must not contain UTF-8 BOM"
    assert b"\r\n" not in raw_bytes, "Rollout must use LF line endings"

    # 2. Validator passes with zero errors
    assert validate_codex_rollout_file(rollout_path) == []

    # 3. Line-by-line conformance against golden native schema
    gen_lines = [json.loads(ln) for ln in raw_bytes.decode("utf-8").splitlines() if ln.strip()]
    line1 = gen_lines[0]
    assert line1["type"] == "session_meta"
    assert line1["ordinal"] == 0
    datetime.fromisoformat(line1["timestamp"].replace("Z", "+00:00"))

    payload = line1["payload"]
    for req in REQUIRED_SESSION_META_KEYS:
        assert payload.get(req), f"Missing required session_meta field '{req}'"
    assert payload["id"] == tid
    assert payload["session_id"] == tid
    # Every core structural key from golden fixture (excluding optional account IDs) is present
    core_golden_keys = golden_meta_keys - {"creator_user_id", "creator_account_id"}
    assert core_golden_keys.issubset(set(payload.keys()))

    # 4. Monotonic ordinals & valid event sequence for subsequent lines
    seen_types = set()
    for expected_ord, obj in enumerate(gen_lines):
        assert obj["ordinal"] == expected_ord
        datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
        t = obj["type"]
        pt = obj["payload"].get("type") if isinstance(obj.get("payload"), dict) else None
        seen_types.add(f"{t}:{pt}" if pt else t)

    assert "event_msg:task_started" in seen_types
    assert "turn_context" in seen_types
    assert "response_item:message" in seen_types
    assert "event_msg:item_completed" in seen_types
    assert "event_msg:task_complete" in seen_types

    # 5. Exact byte offset alignment with thread_history_1.sqlite
    hconn = sqlite3.connect(str(codex.thread_history_db))
    proj_state = hconn.execute(
        "SELECT next_rollout_byte_offset, next_rollout_ordinal FROM thread_history_projection_state WHERE thread_id = ?",
        (tid,),
    ).fetchone()
    turns = hconn.execute(
        "SELECT rollout_byte_offset, rollout_end_byte_offset FROM thread_turns WHERE thread_id = ? ORDER BY rollout_ordinal",
        (tid,),
    ).fetchall()
    hconn.close()
    assert proj_state == (len(raw_bytes), len(gen_lines))
    assert len(turns) == 2
    assert turns[0][0] > 0
    assert turns[-1][1] == len(raw_bytes)


def test_generated_antigravity_chat_conforms_to_hub_pb_and_sqlite_schema(
    sandbox: dict[str, object]
) -> None:
    agy: AntigravityAdapter = sandbox["antigravity"]  # type: ignore[assignment]
    golden_spec = json.loads((FIXTURES_DIR / "antigravity_golden_summary.json").read_text(encoding="utf-8"))

    projs = agy.read_projects()
    app_key = next(k for k in projs if k.endswith("/core-app"))

    chat = ChatSession(
        tool="codex",
        native_id="cx-origin-001",
        title="Antigravity Hub Visibility Test",
        project_key=app_key,
        created_at_ms=1_795_400_000_000,
        updated_at_ms=1_795_400_030_000,
        messages=[
            ChatMessage(role="user", text="Verify Antigravity proto registration.", timestamp_ms=1_795_400_000_000),
            ChatMessage(role="assistant", text="Verified hub summary proto and user_version.", timestamp_ms=1_795_400_030_000),
        ],
    ).compute_hashes()

    cid, _ = agy.write_chat(chat, target_native_id=None, projects_by_key=projs)

    # 1. Per-conversation DB MUST have PRAGMA user_version == 1
    cdb = agy.conversations_dir / f"{cid}.db"
    assert cdb.exists()
    conn = sqlite3.connect(str(cdb))
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    tmb = conn.execute("SELECT data FROM trajectory_metadata_blob WHERE id = 'main'").fetchone()[0]
    steps = conn.execute("SELECT idx, step_type, step_payload FROM steps ORDER BY idx").fetchall()
    conn.close()

    assert uv == golden_spec["expected_conv_db_user_version"]
    decoded_tmb = decode_trajectory_metadata(tmb)
    assert decoded_tmb["cascade_id"] == cid
    assert decoded_tmb["project_id"] == projs[app_key].primary_id("antigravity")
    assert len(steps) == 2
    assert decode_step_text(steps[0][1], steps[0][2])[0] == "Verify Antigravity proto registration."
    assert decode_step_text(steps[1][1], steps[1][2])[0] == "Verified hub summary proto and user_version."

    # 2. conversation_summaries.db row matches native IDLE status + raw_summary fields
    sconn = sqlite3.connect(str(agy.summaries_db))
    srow = sconn.execute(
        "SELECT status, raw_summary, last_user_input_step_index FROM conversation_summaries WHERE conversation_id = ?",
        (cid,),
    ).fetchone()
    sconn.close()
    assert srow is not None
    assert srow[0] == golden_spec["expected_idle_status_string"]
    raw_summary = srow[1]
    fields = parse_wire(raw_summary)
    field_nums = {f for f, _, _ in fields}
    assert set(golden_spec["expected_raw_summary_required_fields"]).issubset(field_nums)
    assert first_varint(fields, 5) == golden_spec["expected_idle_raw_summary_field5"] == RUN_STATUS_IDLE
    assert first_varint(fields, 16) == srow[2] == 0
    assert first_varint(fields, 28) == 4

    # 3. agyhub_summaries_proto.pb MUST contain the conversation with EXACT byte match!
    assert agy.hub_summaries_pb.exists()
    pb_entries = read_agyhub_summaries_pb(agy.hub_summaries_pb)
    assert cid in pb_entries
    assert pb_entries[cid] == raw_summary
    # Doctor reports 0 issues for this conversation
    assert agy.doctor() == []
