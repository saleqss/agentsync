"""Tests for bootstrap migration, Windows path/junction dedup, chat conflict LWW, and strict idempotency."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.codex import CodexAdapter, validate_codex_rollout_file
from agentsync.config import AgentSyncConfig
from agentsync.engine.bootstrap import BootstrapEngine
from agentsync.engine.sync import SyncEngine
from agentsync.models.canonical import ChatMessage, ChatSession
from agentsync.utils.paths import normalize_path, path_dedup_key
from agentsync.utils.proto_wire import read_agyhub_summaries_pb


def _snapshot_tree(roots: list[Path]) -> dict[str, tuple[int, str]]:
    """Return {posix_path: (mtime_ns, sha256)} for all files under roots."""
    snap: dict[str, tuple[int, str]] = {}
    for root in roots:
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal")):
                st = p.stat()
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
                snap[p.as_posix()] = (st.st_mtime_ns, digest)
    return snap


def test_bootstrap_dry_run_makes_zero_disk_changes(sandbox: dict[str, Any]) -> None:
    """bootstrap --from codex --dry-run must report planned mutations while touching zero files."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex: CodexAdapter = sandbox["codex"]
    agy: AntigravityAdapter = sandbox["antigravity"]

    roots = [codex.root_path, agy.root_path, agy.appdata_dir, cfg.resolved_data_dir()]
    before = _snapshot_tree(roots)

    plan = BootstrapEngine(cfg, [codex, agy]).run_from(
        source_tool="codex", dry_run=True, check_processes=False
    )
    after = _snapshot_tree(roots)

    assert before == after
    assert plan.dry_run is True
    assert any(a.category == "skills" and a.action == "DELETE" for a in plan.actions)
    assert any(a.category == "skills" and a.action == "CREATE" for a in plan.actions)
    assert any(a.category == "mcp" and a.action == "SKIP" and a.entity_id == "node_repl" for a in plan.actions)
    assert any(a.category == "mcp" and a.action == "SKIP" and a.entity_id == "legacy_sse_srv" for a in plan.actions)
    assert not cfg.state_db_path.exists()


def test_bootstrap_from_codex_cleans_migrates_and_preserves_protected_state(sandbox: dict[str, Any]) -> None:
    """Live bootstrap clears stale Antigravity skills/MCP/plugins, imports from Codex, preserves auth/settings, and syncs chats."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex: CodexAdapter = sandbox["codex"]
    agy: AntigravityAdapter = sandbox["antigravity"]
    ws_app: Path = sandbox["workspaces"]["app"]

    plan = BootstrapEngine(cfg, [codex, agy]).run_from(
        source_tool="codex", dry_run=False, check_processes=False
    )
    assert plan.backup_id is not None

    # 1. Skills: stale Antigravity skill removed, Codex skills migrated (with synthesized frontmatter)
    agy_skills = agy.read_skills()
    assert "only-agy-old" not in agy_skills
    assert {"shared-skill", "codex-only-skill", "no-frontmatter-skill"} == set(agy_skills)
    assert agy_skills["no-frontmatter-skill"].synthesized_frontmatter is False
    assert (agy.skills_dir / "no-frontmatter-skill" / "SKILL.md").read_text(encoding="utf-8").startswith("---\n")

    # Protected files untouched
    assert (codex.root_path / "auth.json").read_text(encoding="utf-8") == '{"access_token": "secret-codex-auth"}'
    assert (agy.data_dir / "builtin" / "core-builtin" / "SKILL.md").exists()
    assert (codex.skills_dir / ".system" / "openai-docs" / "SKILL.md").exists()
    agy_cfg = json.loads(agy.config_json.read_text(encoding="utf-8"))
    assert agy_cfg["userSettings"]["permissionPreset"] == "AGENT_PERMISSION_PRESET_TURBO"
    assert agy_cfg["sidecars"]["swarm/swarm_board"]["projectId"] == "sidecar-uuid-1"
    assert (agy.projects_dir / "sidecar-uuid-1.json").exists()

    # 2. MCP: stale old-agy-mcp deleted, node_repl and legacy_sse_srv skipped, context7 + motionsites migrated
    agy_mcps = agy.read_mcp_servers()
    assert set(agy_mcps) == {"context7", "motionsites"}
    assert "old-agy-mcp" not in agy_mcps
    assert "node_repl" not in agy_mcps
    assert "legacy_sse_srv" not in agy_mcps

    # 3. Plugins: stale-agy-plugin + empty opencode removed, figma + mattpocock-skills migrated
    agy_plugins = agy.read_plugins()
    assert set(agy_plugins) == {"figma", "mattpocock-skills"}
    assert not (agy.plugins_dir / "stale-agy-plugin").exists()
    assert not (agy.plugins_dir / "opencode").exists()

    # 4. Projects: 3 duplicate Codex rows for core-app collapsed to 1, total 3 projects in both tools
    c_projs = codex.read_projects()
    a_projs = agy.read_projects()
    assert len(c_projs) == 3
    assert len(a_projs) == 3
    assert all(p.duplicate_count("codex") == 0 for p in c_projs.values())

    # 5. Instructions: global and project rules merged cleanly with zero duplicates
    c_global = codex.read_global_instructions()
    a_global = agy.read_global_instructions()
    assert c_global is not None and a_global is not None
    assert c_global.content_hash == a_global.content_hash
    assert "Use pytest." in a_global.raw_text
    assert "Always verify before completion." in a_global.raw_text
    assert (ws_app / "AGENTS.md").read_text(encoding="utf-8") == (ws_app / "GEMINI.md").read_text(encoding="utf-8")

    # 6. Chats: 1 shared duplicate linked (not duplicated!), 2 exclusives copied -> exactly 3 chats in each tool
    c_chats = codex.read_chats(c_projs)
    a_chats = agy.read_chats(a_projs)
    assert len(c_chats) == 3
    assert len(a_chats) == 3

    # Every Codex rollout must pass strict native schema validation
    for rf in codex.sessions_dir.rglob("*.jsonl"):
        assert validate_codex_rollout_file(rf) == []

    # Every Antigravity chat must be registered in agyhub_summaries_proto.pb
    pb_entries = read_agyhub_summaries_pb(agy.hub_summaries_pb)
    assert set(pb_entries.keys()) == {c.native_id for c in a_chats}

    # 7. Post-bootstrap immediate sync must produce strictly 0 mutating actions
    sync_plan = SyncEngine(cfg, [codex, agy]).run(dry_run=False, check_processes=False)
    assert sync_plan.mutating_actions == []


def test_strict_idempotency_across_three_consecutive_syncs(sandbox: dict[str, Any]) -> None:
    """Three consecutive sync runs must leave mtime_ns and SHA-256 of every file byte-identical on runs 2 & 3."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex: CodexAdapter = sandbox["codex"]
    agy: AntigravityAdapter = sandbox["antigravity"]

    engine = SyncEngine(cfg, [codex, agy])
    plan1 = engine.run(dry_run=False, check_processes=False)
    assert len(plan1.mutating_actions) > 0

    roots = [codex.root_path, agy.root_path, agy.appdata_dir, sandbox["workspaces"]["app"]]
    snap_after_1 = _snapshot_tree(roots)

    plan2 = engine.run(dry_run=False, check_processes=False)
    snap_after_2 = _snapshot_tree(roots)
    assert plan2.mutating_actions == []
    assert snap_after_1 == snap_after_2

    plan3 = engine.run(dry_run=False, check_processes=False)
    snap_after_3 = _snapshot_tree(roots)
    assert plan3.mutating_actions == []
    assert snap_after_2 == snap_after_3


def test_windows_path_variants_and_junction_dedup(tmp_path: Path) -> None:
    """Drive casing, slashes, extended prefix, file URI, and NTFS junctions all map to one dedup key."""
    real_dir = tmp_path / "CanonicalProject"
    real_dir.mkdir()

    k_native = path_dedup_key(str(real_dir))
    assert k_native == path_dedup_key(str(real_dir).lower())
    assert k_native == path_dedup_key(str(real_dir).replace("\\", "/") + "/")

    if sys.platform == "win32":
        drive_lower = str(real_dir)[0].lower() + str(real_dir)[1:]
        assert k_native == path_dedup_key(drive_lower)
        assert k_native == path_dedup_key(f"\\\\?\\{real_dir}\\")
        uri = "file:///" + str(real_dir).replace("\\", "/").replace(":", "%3A")
        assert k_native == path_dedup_key(uri)
        assert normalize_path(drive_lower)[0].isupper()

        junction = tmp_path / "JunctionAlias"
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(real_dir)],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and junction.exists():
            try:
                assert path_dedup_key(str(junction)) == k_native
            finally:
                os.rmdir(junction)


def test_chat_lww_conflict_archives_loser_snapshot(sandbox: dict[str, Any]) -> None:
    """When a linked chat diverges on both sides, newer updated_at wins and the overwritten side is archived."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex: CodexAdapter = sandbox["codex"]
    agy: AntigravityAdapter = sandbox["antigravity"]

    engine = SyncEngine(cfg, [codex, agy])
    engine.run(dry_run=False, check_processes=False)

    # Locate the linked duplicate chat on both sides and modify both with different timestamps
    c_projs = codex.read_projects()
    a_projs = agy.read_projects()
    c_chat = next(c for c in codex.read_chats(c_projs) if c.title == "Shared Duplicate Chat")
    a_chat = next(c for c in agy.read_chats(a_projs) if c.title == "Shared Duplicate Chat")

    c_newer = ChatSession(
        tool="codex",
        native_id=c_chat.native_id,
        title=c_chat.title,
        project_key=c_chat.project_key,
        created_at_ms=c_chat.created_at_ms,
        updated_at_ms=c_chat.updated_at_ms + 90_000,
        messages=[
            *c_chat.messages,
            ChatMessage(role="user", text="Codex newer turn wins.", timestamp_ms=c_chat.updated_at_ms + 90_000),
        ],
    ).compute_hashes()
    a_older = ChatSession(
        tool="antigravity",
        native_id=a_chat.native_id,
        title=a_chat.title,
        project_key=a_chat.project_key,
        created_at_ms=a_chat.created_at_ms,
        updated_at_ms=a_chat.updated_at_ms + 10_000,
        messages=[
            *a_chat.messages,
            ChatMessage(role="user", text="Antigravity older conflicting turn.", timestamp_ms=a_chat.updated_at_ms + 10_000),
        ],
    ).compute_hashes()

    codex.write_chat(c_newer, target_native_id=c_chat.native_id, projects_by_key=c_projs)
    agy.write_chat(a_older, target_native_id=a_chat.native_id, projects_by_key=a_projs)

    conflict_plan = engine.run(dry_run=False, check_processes=False)
    assert len(conflict_plan.conflict_actions) == 1
    assert conflict_plan.conflict_actions[0].category == "chats"

    # Conflict snapshot for the chat must exist on disk with the overwritten Antigravity text preserved
    conflict_files = [p for p in cfg.conflicts_dir.rglob("*.json") if "_chats_" in str(p)]
    assert len(conflict_files) == 1
    assert "Antigravity older conflicting turn." in conflict_files[0].read_text(encoding="utf-8")

    # Antigravity now matches the newer Codex conversation
    re_agy = next(c for c in agy.read_chats(a_projs) if c.native_id == a_chat.native_id)
    assert any("Codex newer turn wins." in m.text for m in re_agy.messages)


def test_bootstrap_invalid_source_raises(sandbox: dict[str, Any]) -> None:
    cfg: AgentSyncConfig = sandbox["config"]
    with pytest.raises(ValueError, match="not enabled"):
        BootstrapEngine(cfg, [sandbox["codex"]]).run_from(
            source_tool="claude-code", dry_run=True, check_processes=False
        )
