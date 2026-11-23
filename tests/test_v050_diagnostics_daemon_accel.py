"""Tests for v0.5.0 Auto-Diagnostics, repair --on-close, Split-Tier real-time sync, DirtyFileCache, hot-reload, and accel parity."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentsync import __version__
from agentsync.accel import (
    fingerprint_chat_messages,
    hash_skill_files,
    normalize_text_fast,
    runtime_engine_label,
    scan_rollout_jsonl_fast,
    strip_jsonc_fast,
)
from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.codex import CodexAdapter, validate_codex_rollout_file
from agentsync.cli import app
from agentsync.config import (
    AgentSyncConfig,
    ConfigManager,
    SyncOptions,
    ToolConfig,
    parse_config_value,
    set_config_setting,
)
from agentsync.daemon import SingleInstanceLock
from agentsync.engine.autostart import AutostartStatus, SplitInstallReport
from agentsync.engine.diagnostics import DiagnosticsEngine, load_repair_queue
from agentsync.engine.watcher import DirtyFileCache, SyncWatcher, classify_change_tier
from agentsync.models.canonical import (
    ChatMessage,
    ChatSession,
    SkillItem,
    parse_skill_md,
)
from agentsync.state.backup import BackupManager
from agentsync.utils.jsonc import loads_jsonc
from agentsync.utils.process_guard import RunningAppProcess

runner = CliRunner()


def test_diagnostics_and_deferred_repair_on_close_auto_heals_after_app_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DiagnosticsEngine detects broken Codex rollout & stale lock; SyncWatcher auto-heals after mock Codex exits."""
    codex_root = tmp_path / ".codex"
    agy_root = tmp_path / ".gemini"
    agy_app = tmp_path / "AppData" / "Antigravity"
    for d in (codex_root, agy_root, agy_app):
        d.mkdir(parents=True)

    cfg = AgentSyncConfig(
        data_dir=str(tmp_path / "data"),
        close_grace_seconds=0.2,
        tools={
            "codex": ToolConfig(enabled=True, path=str(codex_root)),
            "antigravity": ToolConfig(enabled=True, path=str(agy_root), extra_paths={"app_data": str(agy_app)}),
        },
    )
    codex = CodexAdapter(cfg.tools["codex"])
    agy = AntigravityAdapter(cfg.tools["antigravity"])

    # Create a broken rollout (missing session_meta line 1) + a stale daemon lockfile
    bad_rollout = codex.sessions_dir / "2026" / "11" / "22" / "rollout-2026-11-22T12-00-00-11111111-2222-3333-4444-555555555555.jsonl"
    bad_rollout.parent.mkdir(parents=True, exist_ok=True)
    bad_rollout.write_text('{"type": "event_msg", "payload": {"type": "user_message", "message": "hello"}}\n', encoding="utf-8")

    # Seed valid state_5.sqlite so integrity check is exercised
    codex.write_chat(
        ChatSession(
            tool="codex",
            native_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            title="Healthy Session",
            created_at_ms=1_795_370_000_000,
            updated_at_ms=1_795_370_001_000,
            messages=[ChatMessage(role="user", text="hi", timestamp_ms=1_795_370_000_000)],
        ).compute_hashes(),
        target_native_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        projects_by_key={},
    )

    cfg.daemon_lock_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_lock_path.write_text(json.dumps({"pid": 999998, "version": "0.4.0"}), encoding="utf-8")

    # Isolate autostart & split-install probes from host machine
    monkeypatch.setattr(
        "agentsync.engine.diagnostics.autostart_status",
        lambda *_a, **_kw: AutostartStatus(
            platform="windows", enabled=False, healthy=True, mechanism="mock", artifact_path="mock"
        ),
    )
    monkeypatch.setattr(
        "agentsync.engine.diagnostics.detect_split_installations",
        lambda *_a, **_kw: SplitInstallReport(current_version=__version__, has_version_drift=False),
    )

    diag = DiagnosticsEngine(cfg, [codex, agy])
    rep = diag.run_all(verify_backups=False)
    codes = {i.code for i in rep.issues}
    assert "DAEMON_STALE_LOCK" in codes
    assert "CODEX_BROKEN_ROLLOUT" in codes

    # Live Windows mandatory lock held by active daemon must NEVER trigger false-positive DAEMON_STALE_LOCK
    active_lock = SingleInstanceLock(tmp_path / "active_data" / "daemon.lock")
    assert active_lock.acquire() is True
    try:
        active_cfg = AgentSyncConfig(data_dir=str(tmp_path / "active_data"), tools={})
        active_rep = DiagnosticsEngine(active_cfg, []).run_all(verify_backups=False)
        assert active_rep.daemon_running is True
        assert "DAEMON_STALE_LOCK" not in {i.code for i in active_rep.issues}
    finally:
        active_lock.release()

    # Queue repairs via queue_repairs_on_close() while Codex is marked "running"
    q = diag.queue_repairs_on_close(rep.issues)
    assert not q.is_empty
    assert cfg.repair_queue_path.exists()

    app_running = {"codex": True}

    def _fake_detector(check_apps: Any = None) -> list[RunningAppProcess]:
        if app_running["codex"]:
            return [RunningAppProcess(app="codex", pid=7777, name="ChatGPT.exe")]
        return []

    watcher = SyncWatcher(
        cfg,
        [codex, agy],
        options=SyncOptions(debounce_seconds=0.2),
        daemon_mode=True,
        run_startup_sync=False,
        poll_interval_seconds=0.05,
        close_grace_seconds=0.12,
        process_detector=_fake_detector,
    )
    watcher.start()
    try:
        watcher.notify_repair_queued()
        time.sleep(0.25)
        # While Codex is still running, bad rollout must NOT be modified yet
        assert bad_rollout.exists()
        assert not load_repair_queue(cfg.repair_queue_path).is_empty

        # Now close Codex! Daemon/watcher should wait grace period, take backup, heal, and clear queue
        app_running["codex"] = False
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if load_repair_queue(cfg.repair_queue_path).is_empty and not cfg.daemon_lock_path.exists():
                break
            time.sleep(0.05)

        assert load_repair_queue(cfg.repair_queue_path).is_empty
        assert not cfg.daemon_lock_path.exists()
        # Verify Codex SQLite DB passes PRAGMA integrity_check post-repair
        con = sqlite3.connect(str(codex.state_db))
        try:
            assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            con.close()
        assert len(BackupManager(cfg).list_backups()) >= 1
    finally:
        watcher.stop()


def test_cli_repair_on_close_persists_repair_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agentsync repair --on-close` writes fixable issues to repair_queue.json without mutating locked tools."""
    codex_root = tmp_path / ".codex"
    codex_root.mkdir()
    cfg_file = tmp_path / "config.toml"
    cfg = AgentSyncConfig(
        data_dir=str(tmp_path / "data"),
        tools={"codex": ToolConfig(enabled=True, path=str(codex_root))},
    )
    ConfigManager(cfg_file).save(cfg)

    bad_rollout = codex_root / "sessions" / "2026" / "11" / "22" / "rollout-bad.jsonl"
    bad_rollout.parent.mkdir(parents=True)
    bad_rollout.write_text('{"bad": true}\n', encoding="utf-8")

    monkeypatch.setattr(
        "agentsync.engine.diagnostics.autostart_status",
        lambda *_a, **_kw: AutostartStatus(
            platform="windows", enabled=False, healthy=True, mechanism="mock", artifact_path="mock"
        ),
    )
    monkeypatch.setattr(
        "agentsync.engine.diagnostics.detect_split_installations",
        lambda *_a, **_kw: SplitInstallReport(current_version=__version__, has_version_drift=False),
    )

    res = runner.invoke(app, ["repair", "--on-close", "--config", str(cfg_file)])
    assert res.exit_code == 0
    assert "Queued" in res.output
    q = load_repair_queue(cfg.repair_queue_path)
    assert any(e.code == "CODEX_BROKEN_ROLLOUT" for e in q.entries)
    assert bad_rollout.exists()


def test_split_tier_realtime_sync_unlocked_tier1_while_app_running_and_hot_reload(tmp_path: Path) -> None:
    """Tier-1 skill edits sync immediately even while Codex is open; DirtyFileCache skips unchanged & suppresses self-writes."""
    # 1. Tier classification
    assert classify_change_tier(Path("/home/u/.codex/skills/demo/SKILL.md")) == "tier1"
    assert classify_change_tier(Path("/home/u/.codex/AGENTS.md")) == "tier1"
    assert classify_change_tier(Path("/home/u/.gemini/antigravity/mcp_config.json")) == "tier1"
    assert classify_change_tier(Path("/home/u/.codex/sqlite/codex.db")) == "tier2"
    assert classify_change_tier(Path("/home/u/.codex/state_5.sqlite-wal")) == "tier2"
    assert classify_change_tier(Path("/home/u/.codex/sessions/2026/11/22/rollout-abc.jsonl")) == "tier2"
    assert classify_change_tier(Path("/home/u/.gemini/antigravity/conversations/a.pb")) == "tier2"
    assert classify_change_tier(Path("/home/u/AppData/Roaming/Antigravity/User/globalStorage/state.vscdb")) == "tier2"

    # 2. DirtyFileCache incremental skip & self-write loop suppression
    cache = DirtyFileCache()
    probe = tmp_path / "skill.md"
    probe.write_text("# v1\n", encoding="utf-8")
    assert cache.is_dirty(str(probe)) is True
    assert cache.is_dirty(str(probe)) is False  # Unchanged mtime/size skipped!
    probe_echo = tmp_path / "echo.md"
    probe_echo.write_text("# written by agentsync\n", encoding="utf-8")
    cache.record_self_write(probe_echo, ttl_seconds=2.0)
    assert cache.is_dirty(str(probe_echo)) is False  # Self-write echo suppressed!
    probe.write_text("# v2 modified content\n", encoding="utf-8")
    assert cache.is_dirty(str(probe)) is True  # Content mutation detected!

    # 3. Tier-1 real-time sync succeeds while ChatGPT.exe is running!
    codex_root = tmp_path / ".codex"
    agy_root = tmp_path / ".gemini"
    agy_app = tmp_path / "AppData" / "Antigravity"
    for d in (codex_root, agy_root, agy_app):
        d.mkdir(parents=True)

    cfg = AgentSyncConfig(
        data_dir=str(tmp_path / "data"),
        realtime_sync_enabled=True,
        power_profile="eco",
        debounce_seconds=2.5,
        tools={
            "codex": ToolConfig(enabled=True, path=str(codex_root)),
            "antigravity": ToolConfig(enabled=True, path=str(agy_root), extra_paths={"app_data": str(agy_app)}),
        },
    )
    codex = CodexAdapter(cfg.tools["codex"])
    agy = AntigravityAdapter(cfg.tools["antigravity"])

    def _always_running(check_apps: Any = None) -> list[RunningAppProcess]:
        return [RunningAppProcess(app="codex", pid=5555, name="ChatGPT.exe")]

    watcher = SyncWatcher(
        cfg,
        [codex, agy],
        options=SyncOptions(debounce_seconds=0.2),
        daemon_mode=True,
        run_startup_sync=False,
        poll_interval_seconds=0.05,
        close_grace_seconds=0.12,
        process_detector=_always_running,
    )
    watcher.start()
    try:
        codex.write_skill(
            SkillItem(
                name="instant-tier1-skill",
                files={"SKILL.md": b"---\nname: instant-tier1-skill\ndescription: Live Tier 1\n---\n# Body\n"},
                source_tool="codex",
                updated_at_ms=1_795_370_000_000,
            ).compute_hashes()
        )
        watcher._handle_change_batch([str(codex.skills_dir / "instant-tier1-skill" / "SKILL.md")])
        # Tier-1 skill MUST already be synced to Antigravity even though ChatGPT.exe is running!
        assert "instant-tier1-skill" in agy.read_skills()
        snap = watcher.status_snapshot()
        assert snap.tier1_sync_count >= 1
        assert snap.pending_queue == []

        # Live hot-reload of power_profile changes effective debounce without restart
        updated_cfg = cfg.model_copy(update={"power_profile": "instant"})
        watcher.reload_config(updated_cfg, [codex, agy])
        assert watcher.config.power_profile == "instant"
        assert watcher.options.debounce_seconds == pytest.approx(0.8)
    finally:
        watcher.stop()


def test_accel_parity_fallback_and_config_value_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """accel.py hot paths match reference outputs bit-for-bit, honor FAST_ANYTHING_DISABLE=1, and config parsing works."""
    # Force pure-Python fallback via env var
    monkeypatch.setenv("FAST_ANYTHING_DISABLE", "1")
    assert runtime_engine_label("auto") == "Pure Python"
    assert runtime_engine_label("python") == "Pure Python"

    # 1. hash_skill_files determinism across key ordering
    h1 = hash_skill_files({"b.txt": b"two", "a.txt": b"one"})
    h2 = hash_skill_files({"a.txt": b"one", "b.txt": b"two"})
    assert h1 == h2 and len(h1) == 64

    # 2. normalize_text_fast & fingerprint_chat_messages
    assert normalize_text_fast("  Hello   \r\n  World \t ") == "Hello World"
    fp1 = fingerprint_chat_messages("proj/demo", [("user", "Hello   world"), ("assistant", "Hi\nthere")])
    fp2 = fingerprint_chat_messages("proj/demo", [("user", "Hello world"), ("assistant", "Hi there")])
    assert fp1 == fp2 and len(fp1) == 64

    # 3. strip_jsonc_fast preserves URLs inside strings and strips //, /* */, trailing commas
    jsonc_sample = """{
      // line comment with https://ignored.example
      "url": "https://api.example.com/v1/*keep*/",
      /* block
         comment */
      "items": [1, 2, 3,],
    }"""
    cleaned = strip_jsonc_fast(jsonc_sample)
    parsed = loads_jsonc(jsonc_sample)
    assert parsed["url"] == "https://api.example.com/v1/*keep*/"
    assert parsed["items"] == [1, 2, 3]
    assert "//" not in cleaned.replace("https://", "")

    # 4. scan_rollout_jsonl_fast parity with validate_codex_rollout_file
    good_rollout = tmp_path / "rollout-ok.jsonl"
    good_rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-11-22T12:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "abc",
                    "timestamp": "2026-11-22T12:00:00Z",
                    "cwd": "/tmp",
                    "originator": "Codex Desktop",
                    "cli_version": "0.115.0",
                    "source": "vscode",
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": "2026-11-22T12:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "hi"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    scan_ok = scan_rollout_jsonl_fast(good_rollout.read_bytes())
    assert scan_ok["parse_error"] is None
    assert scan_ok["has_bom"] is False
    assert scan_ok["line1"]["payload"]["id"] == "abc"
    assert scan_ok["line_count"] == 2
    assert validate_codex_rollout_file(good_rollout) == []

    bom_rollout = tmp_path / "rollout-bom.jsonl"
    bom_rollout.write_bytes(b"\xef\xbb\xbf" + good_rollout.read_bytes())
    scan_bom = scan_rollout_jsonl_fast(bom_rollout.read_bytes())
    assert scan_bom["has_bom"] is True
    bom_errors = validate_codex_rollout_file(bom_rollout)
    assert any("BOM" in e for e in bom_errors)

    # 5. parse_skill_md fast path (flat frontmatter) vs YAML fallback (nested frontmatter)
    flat_md = "---\nname: fast-skill\ndescription: Fast frontmatter\n---\n# Body\n"
    fm_flat, body_flat, had_flat = parse_skill_md(flat_md, "fallback")
    assert had_flat is True
    assert fm_flat["name"] == "fast-skill" and fm_flat["description"] == "Fast frontmatter"
    assert "# Body" in body_flat

    nested_md = "---\nname: nested-skill\ndescription: >\n  Multi-line description\nmeta:\n  author: dev\n---\n# Body\n"
    fm_nest, _body_nest, had_nest = parse_skill_md(nested_md, "fallback")
    assert had_nest is True
    assert fm_nest["name"] == "nested-skill" and "Multi-line description" in str(fm_nest["description"])

    # 6. CLI config value coercion & validation
    assert parse_config_value("realtime_sync_enabled", "false") is False
    assert parse_config_value("debounce_seconds", "1.25") == 1.25
    assert parse_config_value("max_backups", "15") == 15
    cfg = set_config_setting(AgentSyncConfig(), "power_profile", "battery_safe")
    assert cfg.power_profile == "battery_safe"
    with pytest.raises(KeyError):
        parse_config_value("nonexistent_key", "true")
