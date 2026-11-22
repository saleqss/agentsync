"""Tests for semantic translation, env-var syntax, JSONC preservation, Zed/IDE adapters, handoff, selectors, ledger, and board."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from agentsync.adapters.claude_code import ClaudeCodeAdapter
from agentsync.adapters.cline import ClineAdapter
from agentsync.adapters.codex import CodexAdapter
from agentsync.adapters.cursor import CursorAdapter
from agentsync.adapters.gemini_cli import GeminiCliAdapter
from agentsync.adapters.opencode import OpenCodeAdapter
from agentsync.adapters.vscode_copilot import VsCodeCopilotAdapter
from agentsync.adapters.windsurf import WindsurfAdapter
from agentsync.adapters.zed import ZedAdapter
from agentsync.cli import app
from agentsync.config import AgentSyncConfig, ConfigManager, SyncOptions, ToolConfig
from agentsync.engine.board import generate_inventory_board
from agentsync.engine.handoff import HandoffEngine
from agentsync.engine.patch import PatchEngine
from agentsync.engine.skill_export import install_agentsync_skill
from agentsync.engine.sync import SyncEngine
from agentsync.engine.workspace import WorkspaceEngine
from agentsync.models.canonical import InstructionDoc, McpServer, SkillItem
from agentsync.state.ledger import LedgerStore
from agentsync.utils.env_vars import (
    from_canonical_env_vars,
    strip_literal_secrets,
    to_canonical_env_vars,
)
from agentsync.utils.jsonc import loads_jsonc, patch_jsonc_file
from agentsync.utils.selectors import SyncSelector, classify_action_risk
from agentsync.utils.terminology import scan_strict_vocab, translate_host_tokens


def test_semantic_terminology_roundtrip_and_strict_vocab_guard() -> None:
    """Host-specific tokens translate cleanly across Claude <-> Codex <-> Zed and unmapped tokens raise strict-vocab warnings."""
    claude_text = "Read CLAUDE.md, use Bash and TaskCreate, and never call NotebookEdit."
    to_codex = translate_host_tokens(claude_text, "claude-code", "codex", strict_vocab_guard=True)
    assert "AGENTS.md" in to_codex.translated_text
    assert "exec_command" in to_codex.translated_text
    assert "spawn_agent" in to_codex.translated_text
    # NotebookEdit has no Codex equivalent -> strict-vocab guard flags it!
    assert any(w.token == "NotebookEdit" and w.origin_host == "claude-code" for w in to_codex.warnings)

    # Round-trip back to Claude preserves mapped tokens losslessly
    back = translate_host_tokens(to_codex.translated_text, "codex", "claude-code", strict_vocab_guard=True)
    assert "CLAUDE.md" in back.translated_text
    assert "Bash" in back.translated_text
    assert "TaskCreate" in back.translated_text
    assert scan_strict_vocab("Use apply_patch here", target_host="claude-code")


def test_cross_format_env_var_translation_and_strip_secrets() -> None:
    """`${VAR}` <-> `${env:VAR}` <-> `{env:VAR}` translates without expanding secrets, and strip-secrets redacts literals."""
    os.environ["AGENTSYNC_TEST_SECRET"] = "DO_NOT_EXPAND_ME"
    try:
        src = "Bearer ${AGENTSYNC_TEST_SECRET}"
        vs = from_canonical_env_vars(src, "vscode")
        oc = from_canonical_env_vars(src, "opencode")
        assert vs == "Bearer ${env:AGENTSYNC_TEST_SECRET}"
        assert oc == "Bearer {env:AGENTSYNC_TEST_SECRET}"
        assert "DO_NOT_EXPAND_ME" not in vs and "DO_NOT_EXPAND_ME" not in oc
        assert to_canonical_env_vars(vs) == src
        assert to_canonical_env_vars(oc) == src

        stripped = strip_literal_secrets(
            {
                "API_TOKEN": "sk-" + "9999999999abcdef",
                "EXISTING_VAR": "${ALREADY_SAFE}",
                "LOG_LEVEL": "info",
            },
            prefix="ctx7",
        )
        assert stripped["API_TOKEN"] == "${CTX7_API_TOKEN}"
        assert stripped["EXISTING_VAR"] == "${ALREADY_SAFE}"
        assert stripped["LOG_LEVEL"] == "info"
    finally:
        os.environ.pop("AGENTSYNC_TEST_SECRET", None)


def test_jsonc_comment_preservation_for_zed_opencode_and_vscode(tmp_path: Path) -> None:
    """Zed settings.json, OpenCode opencode.jsonc, and VS Code mcp.json preserve JSONC comments and unrelated keys."""
    # 1. Zed adapter
    zed_root = tmp_path / "Zed"
    zed_skills = tmp_path / ".agents" / "skills"
    zed_root.mkdir(parents=True)
    zed_settings = zed_root / "settings.json"
    zed_settings.write_text(
        """{
  // Keep my custom Zed editor theme comment!
  "theme": "One Dark",
  /* Block comment above agent settings */
  "agent": {
    "tool_permissions": {
      "terminal": "confirm"
    }
  },
  "context_servers": {
    "ext-server": {
      "source": "extension",
      "settings": {}
    }
  },
}
""",
        encoding="utf-8",
    )

    zed = ZedAdapter(
        ToolConfig(enabled=True, path=str(zed_root), extra_paths={"skills_dir": str(zed_skills)})
    )
    assert zed.read_tool_permissions() == {"terminal": "confirm"}

    mcps = {
        "context7": McpServer(
            name="context7",
            transport="stdio",
            command="npx",
            args=["-y", "@upstash/context7-mcp"],
            env={"API_KEY": "${CTX7_KEY}"},
            source_tool="codex",
        ).compute_hash()
    }
    changed, warns = zed.write_mcp_servers(mcps)
    assert changed is True and warns == []
    updated_text = zed_settings.read_text(encoding="utf-8")
    assert "// Keep my custom Zed editor theme comment!" in updated_text
    assert "/* Block comment above agent settings */" in updated_text
    parsed = loads_jsonc(updated_text)
    assert parsed["theme"] == "One Dark"
    assert "ext-server" in parsed["context_servers"]  # extension server preserved!
    assert "context7" in parsed["context_servers"]
    assert zed.read_mcp_servers()["context7"].env == {"API_KEY": "${CTX7_KEY}"}

    # Zed skills & project .rules / AGENTS.md
    sk = SkillItem(
        name="zed-skill",
        files={"SKILL.md": b"---\nname: zed-skill\ndescription: demo\n---\n# Zed\n"},
    ).compute_hashes()
    assert zed.write_skill(sk) is True
    assert "zed-skill" in zed.read_skills()
    proj = tmp_path / "zed-proj"
    proj.mkdir()
    zed.write_project_instructions(
        proj, InstructionDoc.from_markdown("# Zed Project Rules\n", scope="project", source_tool="zed")
    )
    assert (proj / ".rules").exists() and (proj / "AGENTS.md").exists()
    assert zed.doctor() == []

    # 2. OpenCode JSONC + {env:VAR} syntax
    oc_cfg = tmp_path / "opencode"
    oc_cfg.mkdir()
    oc_jsonc = oc_cfg / "opencode.jsonc"
    oc_jsonc.write_text("// OpenCode comment\n{\n  \"theme\": \"dark\",\n  \"mcp\": {}\n}\n", encoding="utf-8")
    oc = OpenCodeAdapter(ToolConfig(enabled=True, path=str(oc_cfg), extra_paths={"data_dir": str(tmp_path / "ocdata")}))
    oc.write_mcp_servers(mcps)
    oc_text = oc_jsonc.read_text(encoding="utf-8")
    assert "// OpenCode comment" in oc_text
    assert "{env:CTX7_KEY}" in oc_text
    assert oc.read_mcp_servers()["context7"].env == {"API_KEY": "${CTX7_KEY}"}

    # 3. VS Code / Copilot JSONC + ${env:VAR}
    vs_root = tmp_path / "CodeUser"
    vs_root.mkdir()
    (vs_root / "mcp.json").write_text("// VS Code MCP\n{\n  \"servers\": {}\n}\n", encoding="utf-8")
    vs = VsCodeCopilotAdapter(ToolConfig(enabled=True, path=str(vs_root)))
    vs.write_mcp_servers(mcps)
    vs_text = (vs_root / "mcp.json").read_text(encoding="utf-8")
    assert "// VS Code MCP" in vs_text
    assert "${env:CTX7_KEY}" in vs_text
    assert vs.read_mcp_servers()["context7"].env == {"API_KEY": "${CTX7_KEY}"}

    # 4. Gemini CLI, Cursor, Windsurf, Cline round-trip smoke tests
    for cls, sub in (
        (GeminiCliAdapter, "gemini-cli"),
        (CursorAdapter, "cursor"),
        (WindsurfAdapter, "windsurf"),
        (ClineAdapter, "cline"),
    ):
        adapter = cls(ToolConfig(enabled=True, path=str(tmp_path / sub)))
        adapter.write_mcp_servers(mcps)
        assert "context7" in adapter.read_mcp_servers()
        assert adapter.doctor() == []

    # Direct patch_jsonc_file test adding a brand-new key while keeping comments
    extra_file = tmp_path / "extra.jsonc"
    extra_file.write_text("// header\n{\n  \"a\": 1\n}\n", encoding="utf-8")
    patch_jsonc_file(extra_file, {"b": {"nested": True}})
    assert "// header" in extra_file.read_text(encoding="utf-8")
    assert loads_jsonc(extra_file.read_text(encoding="utf-8")) == {"a": 1, "b": {"nested": True}}


def test_codex_duplicate_agent_skills_and_protected_adapted_skill(sandbox: dict[str, Any]) -> None:
    """Codex doctor flags ~/.codex/skills vs ~/.agents/skills overlap; protected adapted skills survive sync untouched."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex: CodexAdapter = sandbox["codex"]
    agy = sandbox["antigravity"]

    # Create duplicate skill inside ~/.agents/skills mirroring ~/.codex/skills/shared-skill
    agents_dup = codex.root_path.parent / ".agents" / "skills" / "shared-skill"
    agents_dup.mkdir(parents=True)
    (agents_dup / "SKILL.md").write_text("---\nname: shared-skill\ndescription: dup\n---\n# Dup\n", encoding="utf-8")

    issues = codex.doctor()
    assert any(i.code == "CODEX_DUPLICATE_AGENT_SKILL" and i.fixable for i in issues)

    rep = codex.repair(cfg.quarantine_dir, dry_run=False)
    assert rep.fixed_count >= 1
    assert not agents_dup.exists()
    assert not any(i.code == "CODEX_DUPLICATE_AGENT_SKILL" for i in codex.doctor())

    # Mark Antigravity's `shared-skill` as a hand-tuned protected host adaptation
    agy_skill = agy.read_skills()["shared-skill"]
    LedgerStore(cfg).mark_adapted_skill("shared-skill", "antigravity", agy_skill.content_hash)

    # Make Codex's version strictly newer -> sync must preserve Antigravity's adapted version!
    cx_skill_md = codex.skills_dir / "shared-skill" / "SKILL.md"
    future_t = cx_skill_md.stat().st_mtime + 120.0
    os.utime(cx_skill_md, (future_t, future_t))
    plan = SyncEngine(cfg, [codex, agy]).run(dry_run=False, check_processes=False)
    assert any(
        a.category == "skills" and a.action == "SKIP" and a.target_tool == "antigravity" and a.entity_id == "shared-skill"
        for a in plan.skipped_actions
    )
    assert agy.read_skills()["shared-skill"].content_hash == agy_skill.content_hash


def test_session_handoff_db_verification_and_content_hash_dedupe(sandbox: dict[str, Any]) -> None:
    """Session handoff verifies written thread directly in target DB and reuses existing thread on unchanged re-transfer."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex = sandbox["codex"]
    agy = sandbox["antigravity"]
    eng = HandoffEngine(cfg, {"codex": codex, "antigravity": agy})

    prev = eng.preview("codex", "antigravity", session_id="cx-chat-unique")
    assert prev.session_id == "cx-chat-unique"
    assert prev.message_count == 2
    assert prev.reusable_target_id is None

    res1 = eng.execute("codex", "antigravity", session_id="cx-chat-unique", check_processes=False)
    assert res1.verified_on_disk is True
    assert res1.reused_existing is False
    assert res1.verified_message_count == 2

    # Second identical handoff must dedupe via Handoff Import Ledger and reuse the exact same thread ID!
    res2 = eng.execute("codex", "antigravity", session_id="cx-chat-unique", check_processes=False)
    assert res2.reused_existing is True
    assert res2.target_session_id == res1.target_session_id
    assert res2.verified_on_disk is True


def test_selectors_apply_ledger_workspace_board_and_skill_export(sandbox: dict[str, Any], tmp_path: Path) -> None:
    """Selectors, Risk tags, Apply Ledger, Workspace YAML sync, Offline HTML Board, list-skills --json, and install-skill work cleanly."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex = sandbox["codex"]
    agy = sandbox["antigravity"]

    # Selectors & Risk classification
    sel = SyncSelector.parse(["skills:codex-*,mcp"], ["mcp:legacy_*"])
    assert sel.allows("skills", "codex-only-skill") is True
    assert sel.allows("skills", "shared-skill") is False
    assert sel.allows("mcp", "context7") is True
    assert sel.allows("mcp", "legacy_sse_srv") is False
    assert classify_action_risk("skills", "DELETE", "foo") == "high"
    assert classify_action_risk("mcp", "UPDATE", "ctx7") == "medium"

    # PatchEngine with strict targets + selector writes an immutable Apply Ledger
    patch_res = PatchEngine(cfg).run_patch(
        selected_tools=["codex", "antigravity"],
        mode="auto",
        dry_run=False,
        options=SyncOptions(include=["skills:codex-only-skill"]),
        check_processes=False,
    )
    assert patch_res.ledger_path is not None and Path(patch_res.ledger_path).exists()
    ledgers = LedgerStore(cfg).list_apply_ledgers()
    assert len(ledgers) >= 1 and len(ledgers[0].plan_hash) == 64

    # Unified Workspace YAML sync
    ws_dir = tmp_path / "unified-ws"
    ws_dir.mkdir()
    (ws_dir / "AGENTS.md").write_text("# Unified\n\n## Core\n\nRule A.\n", encoding="utf-8")
    (ws_dir / "CLAUDE.md").write_text("# Unified\n\n## Claude Extra\n\nRule B.\n", encoding="utf-8")
    weng = WorkspaceEngine()
    weng.init_workspace(ws_dir)
    wres = weng.sync_workspace(ws_dir)
    assert len(wres.updated_files) >= 2
    assert (ws_dir / "GEMINI.md").exists() and (ws_dir / ".rules").exists()
    assert "Rule A." in (ws_dir / ".rules").read_text(encoding="utf-8")
    assert "Rule B." in (ws_dir / ".rules").read_text(encoding="utf-8")

    # Offline HTML Inventory Board (zero external URLs)
    board_path, row_count = generate_inventory_board([codex, agy], tmp_path / "board.html")
    html_text = board_path.read_text(encoding="utf-8")
    assert row_count > 0
    assert "AGENTSYNC Inventory Board" in html_text
    assert "https://" not in html_text  # strictly offline zero-CDN!

    # Built-in Agent Skill install + CLI list-skills --json
    installed = install_agentsync_skill([codex, agy])
    assert installed == ["codex", "antigravity"]
    assert (codex.skills_dir / "agentsync" / "SKILL.md").exists()

    cfg_file = sandbox["config_file"]
    ConfigManager(cfg_file).save(cfg)
    runner = CliRunner()
    res = runner.invoke(app, ["list-skills", "--json", "--config", str(cfg_file)])
    assert res.exit_code == 0
    parsed_skills = json.loads(res.output)
    assert any(item["name"] == "agentsync" for item in parsed_skills)


def test_claude_plugin_cache_skills_and_broken_symlink_doctor(tmp_path: Path) -> None:
    """ClaudeCodeAdapter discovers cached plugin skills and doctor flags broken symlinks."""
    claude_home = tmp_path / ".claude"
    cache_skill = claude_home / "plugins" / "cache" / "mp" / "pack" / "1.0.0" / "skills" / "cached-sk"
    cache_skill.mkdir(parents=True)
    (cache_skill / "SKILL.md").write_text("---\nname: cached-sk\ndescription: cached\n---\n# Cached\n", encoding="utf-8")

    adapter = ClaudeCodeAdapter(
        ToolConfig(enabled=True, path=str(claude_home), extra_paths={"claude_json": str(tmp_path / ".claude.json")})
    )
    skills = adapter.read_skills()
    assert "cached-sk" in skills
