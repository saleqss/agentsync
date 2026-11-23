"""Seamless in-app self-updater with 6h cached release checks and Windows WinError 32-safe detached helper."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from agentsync import __version__
from agentsync.config import AgentSyncConfig
from agentsync.daemon import start_daemon_background, stop_daemon
from agentsync.engine.autostart import (
    autostart_status,
    detect_split_installations,
    enable_autostart,
    purge_legacy_autostart_artifacts,
)
from agentsync.utils.atomic_io import atomic_write_json

REPO_SLUG = "saleqss/agentsync"
REPO_GIT_URL = f"git+https://github.com/{REPO_SLUG}.git"
RELEASES_LATEST_URL = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
RELEASES_ALL_URL = f"https://api.github.com/repos/{REPO_SLUG}/releases?per_page=10"
RAW_PYPROJECT_URL = f"https://raw.githubusercontent.com/{REPO_SLUG}/main/pyproject.toml"
CACHE_TTL_HOURS = 6

InstallMethod = Literal["uv", "pipx", "editable", "pip"]


class UpdateCheckResult(BaseModel):
    current_version: str
    latest_version: str
    update_available: bool = False
    install_method: InstallMethod = "pip"
    release_url: str = f"https://github.com/{REPO_SLUG}/releases"
    release_notes: str = ""
    checked_at_utc: str = ""
    from_cache: bool = False
    suppressed_by_user: bool = False
    split_install_drift: bool = False
    error: str | None = None


class UpdateApplyResult(BaseModel):
    success: bool
    previous_version: str
    new_version: str | None = None
    install_method: InstallMethod
    command: list[str]
    detached_handoff: bool = False
    helper_script_path: str | None = None
    output: str = ""
    restart_notice: str | None = None
    error: str | None = None


def parse_version_tuple(raw: str) -> tuple[int, int, int, int, int]:
    """Parse a semantic version string like `v0.5.0` or `0.5.0-rc1` into a comparable 5-tuple."""
    cleaned = raw.strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]?(a|alpha|b|beta|rc|dev)\.?(\d*))?$", cleaned, re.IGNORECASE)
    if not m:
        nums = [int(x) for x in re.findall(r"\d+", cleaned)[:3]]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2], 4, 0)
    maj, min_, pat = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre_tag = (m.group(4) or "").lower()
    pre_num = int(m.group(5) or 0)
    rank_map = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3, "": 4}
    return (maj, min_, pat, rank_map.get(pre_tag, 4), pre_num)


def is_newer_version(latest: str, current: str) -> bool:
    return parse_version_tuple(latest) > parse_version_tuple(current)


def detect_install_method(
    *,
    executable: str | None = None,
    package_file: str | None = None,
) -> InstallMethod:
    """Detect whether agentsync was installed via uv tool, pipx, editable source checkout, or pip."""
    exe = (executable or sys.executable or "").replace("\\", "/").lower()
    pkg = (package_file or __file__ or "").replace("\\", "/").lower()

    if "/uv/tools/" in exe or "/uv/tools/" in pkg or ".local/share/uv/" in exe:
        return "uv"
    if "/pipx/venvs/" in exe or "/pipx/venvs/" in pkg:
        return "pipx"

    try:
        pkg_path = Path(package_file or __file__).resolve()
        for parent in pkg_path.parents:
            if (parent / "pyproject.toml").exists() and (parent / "src" / "agentsync").exists():
                return "editable"
    except Exception:
        pass

    return "pip"


def _default_http_get(url: str, timeout: float = 4.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"agentsync/{__version__}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def should_suppress_update_prompt(
    latest_version: str,
    config: AgentSyncConfig | None,
) -> bool:
    """Check whether the user chose Skip Version or Remind Later for `latest_version`."""
    if config is None:
        return False
    if config.skipped_update_version and config.skipped_update_version.lstrip("vV") == latest_version.lstrip("vV"):
        return True
    if config.remind_update_after_utc:
        try:
            until = datetime.fromisoformat(config.remind_update_after_utc.replace("Z", "+00:00"))
            if datetime.now(UTC) < until:
                return True
        except Exception:
            pass
    return False


def check_for_updates(
    cache_path: Path | None = None,
    *,
    current_version: str = __version__,
    force: bool = False,
    include_pre: bool = False,
    config: AgentSyncConfig | None = None,
    fetcher: Callable[[str], str] | None = None,
) -> UpdateCheckResult:
    """Check GitHub Releases (with raw pyproject.toml fallback) using a 6-hour non-blocking cache."""
    method = detect_install_method()
    now = datetime.now(UTC)
    want_pre = include_pre or (config is not None and config.update_channel == "prerelease")

    if not force and cache_path is not None and cache_path.exists():
        try:
            cached = UpdateCheckResult.model_validate_json(cache_path.read_text(encoding="utf-8"))
            if cached.checked_at_utc:
                checked_dt = datetime.fromisoformat(cached.checked_at_utc.replace("Z", "+00:00"))
                if now - checked_dt < timedelta(hours=CACHE_TTL_HOURS) and not cached.error:
                    avail = is_newer_version(cached.latest_version, current_version)
                    return cached.model_copy(
                        update={
                            "current_version": current_version,
                            "update_available": avail,
                            "suppressed_by_user": bool(avail and should_suppress_update_prompt(cached.latest_version, config)),
                            "install_method": method,
                            "from_cache": True,
                        }
                    )
        except Exception:
            pass

    get_fn = fetcher or _default_http_get
    latest_ver: str | None = None
    release_url = f"https://github.com/{REPO_SLUG}/releases"
    release_notes = ""
    err_msg: str | None = None

    try:
        url = RELEASES_ALL_URL if want_pre else RELEASES_LATEST_URL
        raw = get_fn(url)
        data = json.loads(raw)
        rel = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        tag = str(rel.get("tag_name") or "").strip().lstrip("vV")
        if tag:
            latest_ver = tag
            release_url = str(rel.get("html_url") or release_url)
            release_notes = str(rel.get("body") or "")[:1200]
    except Exception as exc:
        err_msg = str(exc)

    if not latest_ver:
        try:
            raw_toml = get_fn(RAW_PYPROJECT_URL)
            parsed = tomllib.loads(raw_toml)
            proj_ver = str((parsed.get("project") or {}).get("version") or "").strip()
            if proj_ver:
                latest_ver = proj_ver
                err_msg = None
        except Exception as exc:
            err_msg = err_msg or str(exc)

    final_latest = latest_ver or current_version
    avail = bool(latest_ver and is_newer_version(final_latest, current_version))
    result = UpdateCheckResult(
        current_version=current_version,
        latest_version=final_latest,
        update_available=avail,
        suppressed_by_user=bool(avail and should_suppress_update_prompt(final_latest, config)),
        install_method=method,
        release_url=release_url,
        release_notes=release_notes,
        checked_at_utc=now.isoformat(),
        from_cache=False,
        error=err_msg if not latest_ver else None,
    )

    if cache_path is not None and not result.error:
        try:
            atomic_write_json(cache_path, result.model_dump(), UpdateCheckResult)
        except Exception:
            pass

    return result


def build_upgrade_command(
    method: InstallMethod,
    *,
    include_pre: bool = False,
    repo_root: Path | None = None,
    python_exe: str | None = None,
) -> list[str]:
    """Return the exact CLI upgrade command matching the detected installation method."""
    py = python_exe or sys.executable
    if method == "uv" and shutil.which("uv"):
        cmd = ["uv", "tool", "install", "--force", REPO_GIT_URL]
        if include_pre:
            cmd.insert(3, "--prerelease=allow")
        return cmd
    if method == "pipx" and shutil.which("pipx"):
        return ["pipx", "install", "--force", REPO_GIT_URL]
    if method == "editable":
        target = str(repo_root) if repo_root else REPO_GIT_URL
        return [py, "-m", "pip", "install", "--upgrade", "-e", target]
    cmd = [py, "-m", "pip", "install", "--upgrade"]
    if include_pre:
        cmd.append("--pre")
    cmd.append(REPO_GIT_URL)
    return cmd


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_windows_detached_updater_script(
    *,
    parent_pid: int,
    upgrade_commands: list[list[str]],
    verify_cmd: list[str],
    log_path: Path,
    refresh_autostart_cmd: list[str] | None = None,
    restart_daemon_cmd: list[str] | None = None,
    relaunch_tui_cmd: list[str] | None = None,
) -> str:
    """Build a pure PowerShell detached self-updater script (ZERO legacy script host dependency).

    Waits for `parent_pid` to exit so Windows releases agentsync.exe (eliminating WinError 32),
    applies the upgrade, verifies --version, refreshes autostart, restarts daemon, and relaunches TUI.
    """
    lines: list[str] = [
        "$ErrorActionPreference = 'Continue'",
        f"$LogFile = {_ps_quote(str(log_path))}",
        "$LogDir = Split-Path -Parent $LogFile",
        "if ($LogDir -and -not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }",
        'function Write-Log([string]$msg) { Add-Content -Path $LogFile -Value ("$(Get-Date -Format o) " + $msg) -Encoding UTF8 }',
        f"Write-Log 'Detached updater started (waiting for parent PID {parent_pid} to exit)...'",
        f"for ($i = 0; $i -lt 60; $i++) {{ if (-not (Get-Process -Id {parent_pid} -ErrorAction SilentlyContinue)) {{ break }}; Start-Sleep -Milliseconds 250 }}",
        "Start-Sleep -Milliseconds 400",
        "$AllOk = $true",
    ]

    for idx, cmd in enumerate(upgrade_commands, start=1):
        exe_q = _ps_quote(cmd[0])
        args_q = ", ".join(_ps_quote(a) for a in cmd[1:])
        lines.append(f"Write-Log 'Step {idx}: running upgrade command...'")
        lines.append(f"$out{idx} = & {exe_q} @({args_q}) 2>&1 | Out-String")
        lines.append(f"$rc{idx} = $LASTEXITCODE")
        lines.append(f"Write-Log ('Exit=' + $rc{idx} + ' ' + $out{idx}.Trim())")
        lines.append(f"if ($rc{idx} -ne 0) {{ $AllOk = $false }}")

    v_exe = _ps_quote(verify_cmd[0])
    v_args = ", ".join(_ps_quote(a) for a in verify_cmd[1:])
    lines.append(f"$verOut = & {v_exe} @({v_args}) 2>&1 | Out-String")
    lines.append("Write-Log ('Verified version: ' + $verOut.Trim())")

    if refresh_autostart_cmd:
        a_exe = _ps_quote(refresh_autostart_cmd[0])
        a_args = ", ".join(_ps_quote(a) for a in refresh_autostart_cmd[1:])
        lines.append(f"if ($AllOk) {{ & {a_exe} @({a_args}) 2>&1 | Out-Null; Write-Log 'Refreshed native autostart task.' }}")

    if restart_daemon_cmd:
        d_exe = _ps_quote(restart_daemon_cmd[0])
        d_args = ", ".join(_ps_quote(a) for a in restart_daemon_cmd[1:])
        lines.append(f"if ($AllOk) {{ & {d_exe} @({d_args}) 2>&1 | Out-Null; Write-Log 'Restarted background daemon.' }}")

    if relaunch_tui_cmd:
        r_exe = _ps_quote(relaunch_tui_cmd[0])
        r_args = ", ".join(_ps_quote(a) for a in relaunch_tui_cmd[1:])
        lines.append(f"if ($AllOk) {{ Start-Process -FilePath {r_exe} -ArgumentList @({r_args}); Write-Log 'Relaunched updated AgentSync.' }}")

    lines.append("Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue")
    return "\n".join(lines) + "\n"


def spawn_windows_detached_update(
    config: AgentSyncConfig,
    *,
    method: InstallMethod | None = None,
    include_pre: bool = False,
    relaunch_tui: bool = False,
    config_path: Path | None = None,
    launcher: Callable[[list[str]], Any] | None = None,
) -> UpdateApplyResult:
    """Spawn a detached PowerShell updater that waits for current PID exit, eliminating WinError 32."""
    chosen = method or detect_install_method()
    repo_root: Path | None = None
    if chosen == "editable":
        for p in Path(__file__).resolve().parents:
            if (p / "pyproject.toml").exists():
                repo_root = p
                break

    # Stop background daemon cleanly before handing off to updater
    daemon_was_running = stop_daemon(config)
    autostart_was_enabled = autostart_status(config, config_path=config_path).enabled

    primary_cmd = build_upgrade_command(chosen, include_pre=include_pre, repo_root=repo_root)
    upgrade_cmds = [primary_cmd]

    # Also unify any split global install if running from an editable checkout
    split_rep = detect_split_installations(config)
    if split_rep.has_version_drift and chosen == "editable" and repo_root is not None:
        global_py = shutil.which("python")
        if global_py and os.path.normcase(global_py) != os.path.normcase(sys.executable):
            upgrade_cmds.append([global_py, "-m", "pip", "install", "--user", "--upgrade", str(repo_root)])

    verify_cmd = [sys.executable, "-m", "agentsync.cli", "--version"]
    refresh_auto = (
        [sys.executable, "-m", "agentsync.cli", "autostart", "enable"]
        if autostart_was_enabled
        else None
    )
    restart_d = (
        [sys.executable, "-m", "agentsync.cli", "daemon", "start"]
        if (daemon_was_running or config.autostart_enabled)
        else None
    )
    relaunch = [sys.executable, "-m", "agentsync.cli"] if relaunch_tui else None

    log_path = config.resolved_data_dir() / "update_last.log"
    script_text = build_windows_detached_updater_script(
        parent_pid=os.getpid(),
        upgrade_commands=upgrade_cmds,
        verify_cmd=verify_cmd,
        log_path=log_path,
        refresh_autostart_cmd=refresh_auto,
        restart_daemon_cmd=restart_d,
        relaunch_tui_cmd=relaunch,
    )

    helper_dir = Path(tempfile.gettempdir())
    helper_script = helper_dir / f"agentsync-updater-{os.getpid()}.ps1"
    helper_script.write_text(script_text, encoding="utf-8")

    ps_cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(helper_script),
    ]

    if launcher is not None:
        launcher(ps_cmd)
    else:
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        subprocess.Popen(
            ps_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )

    return UpdateApplyResult(
        success=True,
        previous_version=__version__,
        install_method=chosen,
        command=primary_cmd,
        detached_handoff=True,
        helper_script_path=str(helper_script),
        restart_notice=(
            "Detached Windows updater started (zero WinError 32 lock). "
            f"Exiting current session now; log at {log_path}."
        ),
    )


def perform_update(
    *,
    config: AgentSyncConfig | None = None,
    method: InstallMethod | None = None,
    include_pre: bool = False,
    detached_on_windows: bool = False,
    relaunch_tui: bool = False,
    config_path: Path | None = None,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> UpdateApplyResult:
    """Execute an in-place or detached Windows-safe upgrade, refreshing autostart and daemon cleanly."""
    if detached_on_windows and sys.platform == "win32" and config is not None and runner is None:
        return spawn_windows_detached_update(
            config,
            method=method,
            include_pre=include_pre,
            relaunch_tui=relaunch_tui,
            config_path=config_path,
        )

    chosen = method or detect_install_method()
    repo_root: Path | None = None
    if chosen == "editable":
        for p in Path(__file__).resolve().parents:
            if (p / "pyproject.toml").exists():
                repo_root = p
                break

    daemon_was_running = stop_daemon(config) if config is not None else False
    autostart_was_enabled = (
        autostart_status(config, config_path=config_path).enabled
        if config is not None
        else False
    )

    cmd = build_upgrade_command(chosen, include_pre=include_pre, repo_root=repo_root)

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=180)

    exec_fn = runner or _run
    try:
        proc = exec_fn(cmd)
    except Exception as exc:
        if daemon_was_running and config is not None:
            start_daemon_background(config, config_path=config_path)
        return UpdateApplyResult(
            success=False,
            previous_version=__version__,
            install_method=chosen,
            command=cmd,
            error=str(exc),
        )

    combined = "\n".join(p for p in (proc.stdout.strip(), proc.stderr.strip()) if p)
    if proc.returncode != 0:
        if daemon_was_running and config is not None:
            start_daemon_background(config, config_path=config_path)
        notice = None
        if sys.platform == "win32" and (
            "access is denied" in combined.lower()
            or "being used by another process" in combined.lower()
            or "winerror 32" in combined.lower()
        ):
            notice = (
                "Windows locked agentsync.exe while running. "
                "Re-run with 'agentsync update --detached' to hand off cleanly after process exit."
            )
        return UpdateApplyResult(
            success=False,
            previous_version=__version__,
            install_method=chosen,
            command=cmd,
            output=combined[:2000],
            restart_notice=notice,
            error=f"Upgrade command exited with status {proc.returncode}",
        )

    # Probe newly installed version in a fresh subprocess
    new_ver: str | None = None
    try:
        vproc = exec_fn([sys.executable, "-m", "agentsync.cli", "--version"])
        if vproc.returncode == 0:
            m = re.search(r"agentsync\s+([^\s]+)", vproc.stdout.strip())
            if m:
                new_ver = m.group(1)
    except Exception:
        pass

    # Clean up legacy autostart artifacts and refresh native autostart + daemon
    if config is not None:
        purge_legacy_autostart_artifacts(config, dry_run=False)
        if autostart_was_enabled:
            try:
                enable_autostart(config, config_path=config_path)
            except Exception:
                pass
        if daemon_was_running:
            start_daemon_background(config, config_path=config_path)

    restart_notice = (
        "Upgrade finished. Restart your terminal or running TUI to load the new version."
        if sys.platform == "win32"
        else None
    )
    return UpdateApplyResult(
        success=True,
        previous_version=__version__,
        new_version=new_ver,
        install_method=chosen,
        command=cmd,
        output=combined[:2000],
        restart_notice=restart_notice,
    )
