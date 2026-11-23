"""Tests for Sync-on-Close watcher queue, autostart generators, self-updater, portable bundles, and support bundle privacy."""

from __future__ import annotations

import json
import subprocess
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agentsync import __version__
from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.codex import CodexAdapter
from agentsync.config import AgentSyncConfig, SyncOptions, ToolConfig
from agentsync.daemon import SingleInstanceLock
from agentsync.engine import autostart as autostart_mod
from agentsync.engine.autostart import (
    build_linux_systemd_unit,
    build_macos_launchd_plist,
    build_windows_task_xml,
    detect_split_installations,
    parse_windows_task_xml,
    resolve_daemon_command,
)
from agentsync.engine.bundle import export_profile_bundle, import_profile_bundle
from agentsync.engine.support import create_support_bundle
from agentsync.engine.updater import (
    build_upgrade_command,
    build_windows_detached_updater_script,
    check_for_updates,
    detect_install_method,
    is_newer_version,
    perform_update,
    should_suppress_update_prompt,
    spawn_windows_detached_update,
)
from agentsync.engine.watcher import SyncWatcher
from agentsync.models.canonical import (
    ChatMessage,
    ChatSession,
    InstructionDoc,
    McpServer,
    SkillItem,
)
from agentsync.state.backup import BackupManager
from agentsync.utils.process_guard import RunningAppProcess


def test_sync_watcher_sync_on_close_deferred_queue(tmp_path: Path) -> None:
    """Changes while apps are running queue safely; once apps close and grace delay passes, sync runs automatically with backup."""
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

    # Simulate Codex Desktop currently running!
    app_is_running = {"codex": True}

    def _fake_detector(check_apps: Any = None) -> list[RunningAppProcess]:
        if app_is_running["codex"]:
            return [RunningAppProcess(app="codex", pid=4242, name="ChatGPT.exe")]
        return []

    watcher = SyncWatcher(
        cfg,
        [codex, agy],
        options=SyncOptions(debounce_seconds=0.2),
        daemon_mode=True,
        run_startup_sync=True,
        poll_interval_seconds=0.05,
        close_grace_seconds=0.12,
        process_detector=_fake_detector,
    )
    watcher.start()
    try:
        # Write a brand-new skill in Codex while Codex is marked "running", then signal a Tier-2 locked DB event
        codex.write_skill(
            SkillItem(
                name="deferred-skill",
                files={"SKILL.md": b"---\nname: deferred-skill\ndescription: Deferred test\n---\n# Body\n"},
                source_tool="codex",
                updated_at_ms=1_795_370_000_000,
            ).compute_hashes()
        )
        # Tier-2 event (SQLite DB) MUST defer while Codex is open!
        watcher._handle_change_batch([str(codex.root_path / "state_5.sqlite")])
        snap_locked = watcher.status_snapshot()
        assert "codex" in snap_locked.running_locked_apps
        assert len(snap_locked.pending_queue) >= 1
        # Antigravity must NOT be written yet while Tier-2 is locked!
        assert "deferred-skill" not in agy.read_skills()

        # Now close Codex! Monitor should wait grace period and auto-sync
        app_is_running["codex"] = False
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if "deferred-skill" in agy.read_skills() and watcher.status_snapshot().sync_count >= 1:
                break
            time.sleep(0.05)

        assert "deferred-skill" in agy.read_skills()
        snap_after = watcher.status_snapshot()
        assert snap_after.sync_count >= 1
        assert snap_after.pending_queue == []
        # Verified daemon.log and pre-sync backup both exist
        assert cfg.daemon_log_path.exists()
        assert len(BackupManager(cfg).list_backups()) >= 1
    finally:
        watcher.stop()


def test_autostart_zero_vbs_native_xml_unicode_split_install_and_lock(tmp_path: Path) -> None:
    """Zero .vbs/wscript anywhere; UTF-16 XML handles Cyrillic paths; split-install & SingleInstanceLock work."""
    # 1. Legacy VBS generator functions must be completely removed
    assert not hasattr(autostart_mod, "build_windows_vbs_launcher")
    assert not hasattr(autostart_mod, "build_windows_schtasks_create_args")

    # 2. Native Windows Task Scheduler UTF-16 XML with Cyrillic/Unicode path
    cyrillic_exe = r"C:\Users\dev\Documents\Синхроннинг\.venv\Scripts\agentsyncw.exe"
    cyrillic_cfg = r"C:\Users\dev\Documents\Синхроннинг\config.toml"
    xml_doc = build_windows_task_xml(
        cyrillic_exe,
        ["--config", cyrillic_cfg],
        working_dir=r"C:\Users\dev\Documents\Синхроннинг",
    )
    low_xml = xml_doc.lower()
    assert ".vbs" not in low_xml
    assert "wscript" not in low_xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml_doc
    assert "<LogonTrigger>" in xml_doc
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml_doc
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml_doc
    assert "<Hidden>true</Hidden>" in xml_doc

    # UTF-16 round-trip preserves Cyrillic characters losslessly
    xml_path = tmp_path / "task.xml"
    xml_path.write_bytes(xml_doc.encode("utf-16"))
    decoded_xml = xml_path.read_bytes().decode("utf-16")
    parsed_cmd, parsed_args = parse_windows_task_xml(decoded_xml)
    assert parsed_cmd == cyrillic_exe
    assert cyrillic_cfg in parsed_args

    # 3. resolve_daemon_command respects native windowless override priority
    exe_w, args_w = resolve_daemon_command(
        config_path=tmp_path / "config.toml",
        executable_override=r"C:\Tools\agentsyncw.exe",
    )
    assert exe_w.endswith("agentsyncw.exe")
    assert "--config" in args_w and "-m" not in args_w

    exe_pyw, args_pyw = resolve_daemon_command(
        executable_override=r"C:\Python313\pythonw.exe",
    )
    assert exe_pyw.endswith("pythonw.exe")
    assert args_pyw[:2] == ["-m", "agentsync.daemon"]

    # 4. macOS plist & Linux systemd unit contain zero .vbs/wscript
    plist = build_macos_launchd_plist("/usr/local/bin/agentsyncw", ["--config", "/tmp/c.toml"], tmp_path / "daemon.log")
    assert "<key>RunAtLoad</key>" in plist
    assert "agentsyncw" in plist
    assert ".vbs" not in plist.lower()

    unit = build_linux_systemd_unit("/usr/bin/agentsyncw", ["--config", "/tmp/c.toml"], tmp_path / "daemon.log")
    assert "ExecStart=" in unit
    assert "Nice=10" in unit
    assert "WantedBy=default.target" in unit
    assert ".vbs" not in unit.lower()

    # 5. Split-installation version drift detection
    old_bin = tmp_path / "global" / "agentsync.exe"
    old_bin.parent.mkdir(parents=True)
    old_bin.write_bytes(b"stub")
    cur_bin = tmp_path / "venv" / "agentsync.exe"
    cur_bin.parent.mkdir(parents=True)
    cur_bin.write_bytes(b"stub")

    def _fake_prober(p: Path) -> str | None:
        return "0.3.0" if "global" in str(p) else __version__

    split = detect_split_installations(
        AgentSyncConfig(data_dir=str(tmp_path / "data")),
        extra_candidates=[(old_bin, "cli"), (cur_bin, "cli")],
        version_prober=_fake_prober,
    )
    assert split.has_version_drift is True
    assert "0.3.0" in split.summary

    # 6. Single-instance OS lockfile mutual exclusion + stale dead-PID reclaim
    lock_file = tmp_path / "daemon.lock"
    lock_file.write_text(json.dumps({"pid": 999999, "version": "0.4.0"}), encoding="utf-8")
    lock1 = SingleInstanceLock(lock_file)
    assert lock1.acquire() is True  # Reclaimed stale dead PID!
    lock2 = SingleInstanceLock(lock_file)
    assert lock2.acquire() is False  # Blocked while lock1 holds active lock!
    lock1.release()
    assert lock2.acquire() is True  # Acquirable again after clean release
    lock2.release()


def test_self_updater_detection_caching_fallback_and_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Self-updater detects install method, caches 6h, respects Remind/Skip, and builds pure-PowerShell detached updater."""
    assert is_newer_version("0.5.0", "0.4.0")
    assert not is_newer_version("0.4.0", "0.4.0")
    assert is_newer_version("0.4.0", "0.4.0-rc1")

    assert detect_install_method(executable=r"C:\Users\dev\.local\share\uv\tools\agentsync\python.exe", package_file=r"C:\uv\tools\a.py") == "uv"
    assert detect_install_method(executable=r"/home/dev/.local/pipx/venvs/agentsync/bin/python", package_file=r"/home/dev/.local/pipx/venvs/agentsync/lib/a.py") == "pipx"
    assert detect_install_method(executable=r"/usr/bin/python3", package_file=str(Path(__file__).resolve().parent.parent / "src" / "agentsync" / "engine" / "updater.py")) == "editable"
    assert detect_install_method(executable=r"/usr/bin/python3", package_file=r"/usr/lib/python3.13/site-packages/agentsync/engine/updater.py") == "pip"

    # Remind Later & Skip Version suppression
    cfg_skip = AgentSyncConfig(skipped_update_version="0.9.0")
    assert should_suppress_update_prompt("0.9.0", cfg_skip) is True
    assert should_suppress_update_prompt("0.9.1", cfg_skip) is False
    cfg_rem = AgentSyncConfig(remind_update_after_utc=(datetime.now(UTC) + timedelta(hours=12)).isoformat())
    assert should_suppress_update_prompt("0.9.0", cfg_rem) is True
    cfg_past = AgentSyncConfig(remind_update_after_utc=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    assert should_suppress_update_prompt("0.9.0", cfg_past) is False

    cache_file = tmp_path / "update_cache.json"
    calls = {"n": 0}

    def _mock_fetch_release(url: str) -> str:
        calls["n"] += 1
        return json.dumps({"tag_name": "v0.9.0", "html_url": "https://github.com/saleqss/agentsync/releases/tag/v0.9.0", "body": "New features"})

    r1 = check_for_updates(cache_file, current_version="0.4.0", force=False, fetcher=_mock_fetch_release)
    assert r1.update_available is True
    assert r1.latest_version == "0.9.0"
    assert r1.from_cache is False
    assert calls["n"] == 1

    # Second call within 6h hits local cache without calling fetcher
    r2 = check_for_updates(cache_file, current_version="0.4.0", force=False, fetcher=_mock_fetch_release)
    assert r2.from_cache is True
    assert r2.latest_version == "0.9.0"
    assert calls["n"] == 1

    # Fallback to raw pyproject.toml when Releases API fails
    def _mock_fallback(url: str) -> str:
        if "api.github.com" in url:
            raise OSError("404 Not Found")
        return '[project]\nname = "agentsync"\nversion = "0.8.2"\n'

    r3 = check_for_updates(tmp_path / "c2.json", current_version="0.4.0", force=True, fetcher=_mock_fallback)
    assert r3.update_available is True
    assert r3.latest_version == "0.8.2"

    # Offline resilience: never crashes when network is unavailable
    def _mock_offline(_url: str) -> str:
        raise OSError("Network unreachable")

    r_off = check_for_updates(tmp_path / "c3.json", current_version="0.4.0", force=True, fetcher=_mock_offline)
    assert r_off.update_available is False
    assert r_off.error is not None

    # Mock perform_update success
    cmd = build_upgrade_command("pip")
    assert "pip" in cmd
    upd_ok = perform_update(
        method="pip",
        runner=lambda args: subprocess.CompletedProcess(
            args, 0, stdout="agentsync 0.9.0\n" if "--version" in args else "Successfully installed agentsync-0.9.0\n", stderr=""
        ),
    )
    assert upd_ok.success is True
    assert upd_ok.new_version == "0.9.0"

    # Windows detached PowerShell updater script (zero .vbs / wscript!)
    ps_script = build_windows_detached_updater_script(
        parent_pid=12345,
        upgrade_commands=[[r"C:\Python313\python.exe", "-m", "pip", "install", "--upgrade", "agentsync"]],
        verify_cmd=[r"C:\Python313\python.exe", "-m", "agentsync.cli", "--version"],
        log_path=tmp_path / "update_last.log",
        refresh_autostart_cmd=[r"C:\Python313\python.exe", "-m", "agentsync.cli", "autostart", "enable"],
        restart_daemon_cmd=[r"C:\Python313\python.exe", "-m", "agentsync.cli", "daemon", "start"],
        relaunch_tui_cmd=[r"C:\Python313\python.exe", "-m", "agentsync.cli"],
    )
    assert "Get-Process -Id 12345" in ps_script
    assert "--version" in ps_script
    assert "autostart" in ps_script
    assert "daemon" in ps_script
    assert ".vbs" not in ps_script.lower()
    assert "wscript" not in ps_script.lower()

    # spawn_windows_detached_update writes a .ps1 helper and invokes mocked launcher cleanly
    launched: list[list[str]] = []
    monkeypatch.setattr("agentsync.engine.updater.stop_daemon", lambda _c: False)
    monkeypatch.setattr(
        "agentsync.engine.updater.autostart_status",
        lambda *_a, **_kw: autostart_mod.AutostartStatus(
            platform="windows", enabled=False, healthy=True, mechanism="mock", artifact_path="mock"
        ),
    )
    monkeypatch.setattr(
        "agentsync.engine.updater.detect_split_installations",
        lambda _c: autostart_mod.SplitInstallReport(current_version=__version__, has_version_drift=False),
    )
    det = spawn_windows_detached_update(
        AgentSyncConfig(data_dir=str(tmp_path / "upd_data")),
        method="pip",
        launcher=lambda args: launched.append(args),
    )
    assert det.success is True
    assert det.detached_handoff is True
    assert det.helper_script_path is not None and det.helper_script_path.endswith(".ps1")
    assert Path(det.helper_script_path).exists()
    assert len(launched) == 1 and "powershell.exe" in launched[0][0].lower()
    Path(det.helper_script_path).unlink(missing_ok=True)


def test_portable_profile_bundle_secret_redaction_and_sha256_tamper_guard(tmp_path: Path) -> None:
    """Bundle export redacts literal secrets by default, verifies SHA-256 on import, and rejects tampered zips."""
    src_root = tmp_path / "src_codex"
    dst_root = tmp_path / "dst_codex"
    src_root.mkdir()
    dst_root.mkdir()

    src = CodexAdapter(ToolConfig(enabled=True, path=str(src_root)))
    dst = CodexAdapter(ToolConfig(enabled=True, path=str(dst_root)))

    src.write_skill(
        SkillItem(
            name="portable-skill",
            files={"SKILL.md": b"---\nname: portable-skill\ndescription: Test skill\n---\n# Hello\n"},
            source_tool="codex",
            updated_at_ms=1_795_370_000_000,
        ).compute_hashes()
    )
    secret_val = "sk-proj-SUPERSECRETVALUE1234567890"
    src.write_mcp_servers(
        {
            "openai-tools": McpServer(
                name="openai-tools",
                transport="stdio",
                command="npx",
                args=["-y", "demo-mcp"],
                env={"OPENAI_API_KEY": secret_val},
                source_tool="codex",
                updated_at_ms=1_795_370_000_000,
            ).compute_hash()
        }
    )
    src.write_global_instructions(
        InstructionDoc.from_markdown("# Rules\n\n- Be thorough.\n", scope="global", source_tool="codex", updated_at_ms=1_795_370_000_000)
    )

    bundle_zip = tmp_path / "profile.zip"
    mf = export_profile_bundle(bundle_zip, [src], include_secrets=False)
    assert mf.secrets_redacted is True
    assert mf.skill_count == 1
    assert mf.mcp_count == 1
    assert mf.has_global_instructions is True

    # Confirm literal secret does NOT appear anywhere inside the default zip
    with zipfile.ZipFile(bundle_zip, "r") as zf:
        for name in zf.namelist():
            assert secret_val not in zf.read(name).decode("utf-8", errors="replace")

    # Import into fresh destination tool
    cfg = AgentSyncConfig(data_dir=str(tmp_path / "data"), tools={"codex": ToolConfig(enabled=True, path=str(dst_root))})
    res = import_profile_bundle(bundle_zip, cfg, [dst], dry_run=False, force=True, check_processes=False)
    assert res.skills_imported == 1
    assert res.mcp_imported == 1
    assert res.backup_id is not None
    assert "portable-skill" in dst.read_skills()
    assert "openai-tools" in dst.read_mcp_servers()
    assert dst.read_global_instructions() is not None

    # Tamper with an entry inside zip and verify import refuses to touch disk
    tampered_zip = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle_zip, "r") as zin, zipfile.ZipFile(tampered_zip, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename.endswith("SKILL.md"):
                data = b"MALICIOUS PAYLOAD"
            zout.writestr(item, data)

    with pytest.raises(ValueError, match="Corrupted bundle entry"):
        import_profile_bundle(tampered_zip, cfg, [dst], dry_run=False, force=True, check_processes=False)


def test_support_bundle_strictly_strips_secrets_pii_and_chats(tmp_path: Path) -> None:
    """Support bundle contains diagnostics while stripping API keys, bearer tokens, emails, usernames, and all chat contents."""
    codex_root = tmp_path / "Users" / "SecretUser99" / ".codex"
    codex_root.mkdir(parents=True)
    cfg = AgentSyncConfig(
        data_dir=str(tmp_path / "data"),
        tools={"codex": ToolConfig(enabled=True, path=str(codex_root))},
    )
    adapter = CodexAdapter(cfg.tools["codex"])
    # Seed a chat with confidential text that must NEVER leak into support bundle
    adapter.write_chat(
        ChatSession(
            tool="codex",
            native_id="99999999-1111-2222-3333-444444444444",
            title="PRIVATE_CHAT_TITLE_XYZ",
            created_at_ms=1_795_370_000_000,
            updated_at_ms=1_795_370_001_000,
            messages=[ChatMessage(role="user", text="TOP_SECRET_CONVERSATION_BODY_777", timestamp_ms=1_795_370_000_000)],
        ).compute_hashes(),
        target_native_id="99999999-1111-2222-3333-444444444444",
        projects_by_key={},
    )
    # Write sensitive strings into daemon.log to verify anonymization
    cfg.daemon_log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.daemon_log_path.write_text(
        "2026-11-22 [INFO] user=SecretUser99 email=alice.dev@corp.example "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.SECRET "
        "api_key=sk-ant-api03-SECRETKEY999999999 ip=192.168.1.44\n",
        encoding="utf-8",
    )

    out_zip = tmp_path / "support.zip"
    create_support_bundle(out_zip, cfg, [adapter], extra_usernames=["SecretUser99"])
    assert out_zip.exists()

    forbidden = [
        "SecretUser99",
        "alice.dev@corp.example",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "sk-ant-api03-SECRETKEY999999999",
        "192.168.1.44",
        "PRIVATE_CHAT_TITLE_XYZ",
        "TOP_SECRET_CONVERSATION_BODY_777",
    ]
    with zipfile.ZipFile(out_zip, "r") as zf:
        names = set(zf.namelist())
        assert {"environment.json", "doctor.json", "config_redacted.json", "daemon_tail.log", "PRIVACY_NOTICE.txt"}.issubset(names)
        for name in names:
            content = zf.read(name).decode("utf-8", errors="replace")
            for bad in forbidden:
                assert bad not in content, f"Leaked '{bad}' inside {name}!"
