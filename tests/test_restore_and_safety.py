"""Tests for backup/restore integrity, secret masking, process guard, atomic IO, and CLI end-to-end flows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentsync import __version__
from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.codex import CodexAdapter
from agentsync.cli import app
from agentsync.config import AgentSyncConfig, ConfigManager
from agentsync.engine.bootstrap import BootstrapEngine
from agentsync.state.backup import BackupManager
from agentsync.utils.atomic_io import atomic_write_text
from agentsync.utils.process_guard import classify_process
from agentsync.utils.secrets import mask_mapping, mask_text

runner = CliRunner()


def test_backup_restore_and_sha256_corruption_detection(sandbox: dict[str, Any]) -> None:
    """restore <backup_id> rolls back bootstrap mutations bit-for-bit and aborts if a backup file is tampered."""
    cfg: AgentSyncConfig = sandbox["config"]
    codex: CodexAdapter = sandbox["codex"]
    agy: AntigravityAdapter = sandbox["antigravity"]

    orig_agy_skills = set(agy.read_skills())
    orig_agy_mcps = set(agy.read_mcp_servers())
    orig_agy_cfg = agy.config_json.read_text(encoding="utf-8")

    plan = BootstrapEngine(cfg, [codex, agy]).run_from(
        source_tool="codex", dry_run=False, check_processes=False
    )
    assert plan.backup_id is not None
    assert "only-agy-old" not in agy.read_skills()
    assert "old-agy-mcp" not in agy.read_mcp_servers()

    # Restore exact pre-bootstrap state
    bmgr = BackupManager(cfg)
    res = bmgr.restore_backup(
        plan.backup_id,
        adapters_by_name={"codex": codex, "antigravity": agy},
        check_processes=False,
    )
    assert res.restored_files > 0
    assert set(agy.read_skills()) == orig_agy_skills
    assert set(agy.read_mcp_servers()) == orig_agy_mcps
    assert agy.config_json.read_text(encoding="utf-8") == orig_agy_cfg

    # Tamper with one backed-up payload and assert SHA-256 verification aborts restore
    backup_dir = cfg.backups_dir / plan.backup_id
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    target_payload = backup_dir / manifest["entries"][0]["backup_rel_path"]
    target_payload.write_bytes(b"corrupted-bytes")

    with pytest.raises(ValueError, match="Corrupted backup"):
        bmgr.restore_backup(plan.backup_id, check_processes=False)


def test_secret_masking_and_regex_safety() -> None:
    """Secrets in text and nested mappings are masked reliably without throwing regex flag errors."""
    fake_api = "sk-" + "1234567890abcdef"
    fake_gh = "ghp_" + "a" * 32
    raw = f"Authorization: Bearer {fake_api} token={fake_gh}"
    masked = mask_text(raw)
    assert fake_api not in masked
    assert fake_gh not in masked
    assert "***" in masked

    env_masked = mask_mapping(
        {
            "CONTEXT7_API_KEY": "sk-secret-value-1234567890abcdef",
            "SAFE_FLAG": "true",
            "nested": {"authorization": "Basic dXNlcjpwYXNz"},
        }
    )
    assert env_masked["SAFE_FLAG"] == "true"
    assert "sk-secret" not in str(env_masked["CONTEXT7_API_KEY"])
    assert "dXNlcjpwYXNz" not in str(env_masked["nested"]["authorization"])


def test_process_guard_classifies_all_five_tools_and_excludes_sandbox_service() -> None:
    """Process guard detects Codex MSIX, Antigravity, Claude Code, OpenCode, DeepSeek Harness, and ignores sandbox service."""
    assert (
        classify_process(
            "ChatGPT.exe",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.32239.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe",
        )
        == "codex"
    )
    assert classify_process("codex.exe", r"C:\Users\dev\.codex\codex.exe") == "codex"
    assert (
        classify_process(
            "codex-windows-sandbox-service.exe",
            r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1103.32239.0_x64__2p2nqsd0c76g0\app\codex-windows-sandbox-service.exe",
        )
        is None
    )
    assert classify_process("Antigravity.exe", r"C:\Users\dev\AppData\Local\Programs\Antigravity\Antigravity.exe") == "antigravity"
    assert (
        classify_process(
            "language_server.exe",
            r"C:\Users\dev\AppData\Local\Programs\Antigravity\resources\app\language_server.exe",
        )
        == "antigravity"
    )
    assert classify_process("claude.exe", r"C:\Users\dev\.claude\bin\claude.exe") == "claude-code"
    assert classify_process("opencode.exe", r"C:\Users\dev\AppData\Local\opencode\opencode.exe") == "opencode"
    assert (
        classify_process(
            "DeepSeek Harness.exe",
            r"C:\Users\dev\AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe",
        )
        == "deepseek-harness"
    )
    assert classify_process("explorer.exe", r"C:\Windows\explorer.exe") is None


def test_atomic_write_validator_prevents_partial_corruption(tmp_path: Path) -> None:
    """atomic_write_text preserves the original file when validator raises an exception."""
    target = tmp_path / "config.json"
    target.write_text('{"ok": true}\n', encoding="utf-8")

    def _reject(_text: str) -> None:
        raise ValueError("schema validation failed")

    with pytest.raises(ValueError, match="schema validation failed"):
        atomic_write_text(target, "INVALID", validator=_reject)

    assert target.read_text(encoding="utf-8") == '{"ok": true}\n'


def test_cli_end_to_end_commands(sandbox: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI commands work end-to-end against an isolated sandbox config without touching real user paths."""
    cfg: AgentSyncConfig = sandbox["config"]
    cfg_file: Path = sandbox["config_file"]
    ConfigManager(cfg_file).save(cfg)

    # Mock running-process detection to return empty inside CLI tests
    monkeypatch.setattr("agentsync.engine.sync.assert_apps_closed", lambda **_kw: [])
    monkeypatch.setattr("agentsync.engine.bootstrap.assert_apps_closed", lambda **_kw: [])
    monkeypatch.setattr("agentsync.engine.doctor.assert_apps_closed", lambda **_kw: [])
    monkeypatch.setattr("agentsync.state.backup.assert_apps_closed", lambda **_kw: [])
    monkeypatch.setattr("agentsync.cli.detect_running_apps", lambda **_kw: [])

    # --version
    res_ver = runner.invoke(app, ["--version"])
    assert res_ver.exit_code == 0
    assert __version__ in res_ver.output

    # tools list / enable / disable / set-entities
    res_list = runner.invoke(app, ["tools", "list", "--config", str(cfg_file)])
    assert res_list.exit_code == 0
    assert "codex" in res_list.output
    assert "antigravity" in res_list.output

    res_en = runner.invoke(app, ["tools", "enable", "claude-code", "--config", str(cfg_file)])
    assert res_en.exit_code == 0
    assert ConfigManager(cfg_file).load().tools["claude-code"].enabled is True

    res_dis = runner.invoke(app, ["tools", "disable", "claude-code", "--config", str(cfg_file)])
    assert res_dis.exit_code == 0
    assert ConfigManager(cfg_file).load().tools["claude-code"].enabled is False

    res_ent = runner.invoke(app, ["tools", "set-entities", "codex", "projects,skills,chats", "--config", str(cfg_file)])
    assert res_ent.exit_code == 0
    assert ConfigManager(cfg_file).load().tools["codex"].entities == ["projects", "skills", "chats"]
    # Restore all entities for subsequent bootstrap/sync tests
    runner.invoke(
        app,
        ["tools", "set-entities", "codex", "projects,instructions,skills,mcp,plugins,chats", "--config", str(cfg_file)],
    )

    # audit
    res_audit = runner.invoke(app, ["audit", "--config", str(cfg_file)])
    assert res_audit.exit_code == 0
    assert "Multi-Tool Entity Audit" in res_audit.output

    # doctor
    res_doc = runner.invoke(app, ["doctor", "--config", str(cfg_file)])
    assert res_doc.exit_code == 0
    assert "Doctor Diagnostics (1 issue(s) found)" in res_doc.output
    assert "duplicate rows" in res_doc.output

    # bootstrap --from codex --dry-run
    res_bdry = runner.invoke(app, ["bootstrap", "--from", "codex", "--dry-run", "--config", str(cfg_file)])
    assert res_bdry.exit_code == 0
    assert "DRY-RUN" in res_bdry.output

    # bootstrap --from codex (live on sandbox)
    res_boot = runner.invoke(app, ["bootstrap", "--from", "codex", "--config", str(cfg_file)])
    assert res_boot.exit_code == 0
    assert "APPLIED" in res_boot.output

    # sync (0 diff after bootstrap)
    res_sync = runner.invoke(app, ["sync", "--config", str(cfg_file)])
    assert res_sync.exit_code == 0
    assert "0 diff" in res_sync.output

    # status
    res_status = runner.invoke(app, ["status", "--config", str(cfg_file)])
    assert res_status.exit_code == 0
    assert "Available Backups" in res_status.output

    # restore latest backup
    backups = BackupManager(cfg).list_backups()
    assert len(backups) == 1
    res_restore = runner.invoke(app, ["restore", backups[0].backup_id, "--config", str(cfg_file)])
    assert res_restore.exit_code == 0
    assert "Restored backup" in res_restore.output
