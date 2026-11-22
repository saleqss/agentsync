"""Cross-platform unattended background autostart management (Windows, macOS, Linux)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agentsync.config import AgentSyncConfig
from agentsync.utils.atomic_io import atomic_write_text

WINDOWS_TASK_NAME = "AgentSyncDaemon"
MACOS_LABEL = "io.github.saleqss.agentsync.daemon"
LINUX_SERVICE_NAME = "agentsync.service"


class AutostartStatus(BaseModel):
    platform: Literal["windows", "macos", "linux"]
    enabled: bool = False
    mechanism: str
    artifact_path: str
    executable: str = ""
    detail: str = ""


def resolve_agentsync_command(
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> list[str]:
    """Return the exact argv list to launch `agentsync watch --daemon` reliably."""
    exe = shutil.which("agentsync")
    if exe:
        cmd = [exe, "watch", "--daemon"]
    else:
        py = sys.executable
        if sys.platform == "win32":
            pyw = Path(py).with_name("pythonw.exe")
            if pyw.exists():
                py = str(pyw)
        cmd = [py, "-m", "agentsync.cli", "watch", "--daemon"]
    if config_path is not None:
        cmd.extend(["--config", str(config_path)])
    if data_dir is not None:
        cmd.extend(["--data-dir", str(data_dir)])
    return cmd


def build_windows_vbs_launcher(argv: list[str]) -> str:
    """Build a hidden VBScript wrapper that starts the daemon with zero flashing console window."""
    quoted = " ".join(f'""{arg}""' if " " in arg else arg for arg in argv)
    return (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run "{quoted}", 0, False\n'
    )


def build_windows_schtasks_create_args(vbs_path: Path, task_name: str = WINDOWS_TASK_NAME) -> list[str]:
    """Build user-level logon Scheduled Task arguments (requires NO Administrator elevation)."""
    return [
        "schtasks",
        "/Create",
        "/SC",
        "ONLOGON",
        "/TN",
        task_name,
        "/TR",
        f'wscript.exe "{vbs_path}"',
        "/RL",
        "LIMITED",
        "/F",
    ]


def build_windows_schtasks_delete_args(task_name: str = WINDOWS_TASK_NAME) -> list[str]:
    return ["schtasks", "/Delete", "/TN", task_name, "/F"]


def build_macos_launchd_plist(
    argv: list[str],
    log_path: Path,
    label: str = MACOS_LABEL,
) -> str:
    """Generate a macOS ~/Library/LaunchAgents plist XML document."""
    args_xml = "\n".join(f"        <string>{a}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def build_linux_systemd_unit(argv: list[str], log_path: Path) -> str:
    """Generate a Linux ~/.config/systemd/user/agentsync.service unit file."""
    exec_start = " ".join(f'"{a}"' if " " in a else a for a in argv)
    return f"""[Unit]
Description=AgentSync Background Synchronization Daemon
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
StandardOutput=append:{log_path}
StandardError=append:{log_path}

[Install]
WantedBy=default.target
"""


def _platform_kind() -> Literal["windows", "macos", "linux"]:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _artifact_path_for(config: AgentSyncConfig, plat: Literal["windows", "macos", "linux"]) -> Path:
    if plat == "windows":
        return config.resolved_data_dir() / "agentsync-daemon.vbs"
    if plat == "macos":
        return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "systemd" / "user" / LINUX_SERVICE_NAME


def autostart_status(
    config: AgentSyncConfig,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> AutostartStatus:
    """Inspect whether unattended autostart is currently registered on this machine (read-only)."""
    plat = _platform_kind()
    artifact = _artifact_path_for(config, plat)
    cmd = resolve_agentsync_command(config_path=config_path, data_dir=data_dir)
    exe_str = " ".join(cmd)

    if plat == "windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=8,
                creationflags=creationflags,
            )
            active = proc.returncode == 0
            detail = (
                f"Scheduled Task '{WINDOWS_TASK_NAME}' active (logon trigger)"
                if active
                else "Not registered in Windows Task Scheduler"
            )
        except Exception as exc:
            active = False
            detail = f"Unable to query schtasks: {exc}"
        return AutostartStatus(
            platform="windows",
            enabled=active,
            mechanism="Windows Task Scheduler (ONLOGON, hidden)",
            artifact_path=str(artifact),
            executable=exe_str,
            detail=detail,
        )

    if plat == "macos":
        active = artifact.exists()
        return AutostartStatus(
            platform="macos",
            enabled=active,
            mechanism="macOS launchd LaunchAgent",
            artifact_path=str(artifact),
            executable=exe_str,
            detail="LaunchAgent plist installed" if active else "LaunchAgent not installed",
        )

    active = artifact.exists()
    detail = "systemd --user unit installed" if active else "systemd --user unit not installed"
    if active and shutil.which("systemctl"):
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-enabled", LINUX_SERVICE_NAME],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if proc.returncode == 0:
                detail = f"systemd --user unit enabled ({proc.stdout.strip()})"
        except Exception:
            pass
    return AutostartStatus(
        platform="linux",
        enabled=active,
        mechanism="Linux systemd --user service",
        artifact_path=str(artifact),
        executable=exe_str,
        detail=detail,
    )


def enable_autostart(
    config: AgentSyncConfig,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> AutostartStatus:
    """Register unattended background autostart on login without requiring admin privileges."""
    plat = _platform_kind()
    artifact = _artifact_path_for(config, plat)
    cmd = resolve_agentsync_command(config_path=config_path, data_dir=data_dir)
    log_path = config.daemon_log_path

    if plat == "windows":
        vbs = build_windows_vbs_launcher(cmd)
        atomic_write_text(artifact, vbs)
        args = build_windows_schtasks_create_args(artifact)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to register Windows Scheduled Task '{WINDOWS_TASK_NAME}': "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return AutostartStatus(
            platform="windows",
            enabled=True,
            mechanism="Windows Task Scheduler (ONLOGON, hidden)",
            artifact_path=str(artifact),
            executable=" ".join(cmd),
            detail=f"Registered hidden logon task '{WINDOWS_TASK_NAME}'",
        )

    if plat == "macos":
        plist = build_macos_launchd_plist(cmd, log_path)
        atomic_write_text(artifact, plist)
        if shutil.which("launchctl"):
            subprocess.run(["launchctl", "unload", str(artifact)], capture_output=True, timeout=10)
            subprocess.run(["launchctl", "load", str(artifact)], capture_output=True, timeout=10)
        return AutostartStatus(
            platform="macos",
            enabled=True,
            mechanism="macOS launchd LaunchAgent",
            artifact_path=str(artifact),
            executable=" ".join(cmd),
            detail="Installed and loaded LaunchAgent",
        )

    unit = build_linux_systemd_unit(cmd, log_path)
    atomic_write_text(artifact, unit)
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", LINUX_SERVICE_NAME],
            capture_output=True,
            timeout=10,
        )
    return AutostartStatus(
        platform="linux",
        enabled=True,
        mechanism="Linux systemd --user service",
        artifact_path=str(artifact),
        executable=" ".join(cmd),
        detail="Installed and enabled systemd --user service",
    )


def disable_autostart(config: AgentSyncConfig) -> AutostartStatus:
    """Unregister background autostart cleanly across Windows, macOS, and Linux."""
    plat = _platform_kind()
    artifact = _artifact_path_for(config, plat)

    if plat == "windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            build_windows_schtasks_delete_args(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        if artifact.exists():
            artifact.unlink(missing_ok=True)
        return AutostartStatus(
            platform="windows",
            enabled=False,
            mechanism="Windows Task Scheduler (ONLOGON, hidden)",
            artifact_path=str(artifact),
            detail=f"Removed Scheduled Task '{WINDOWS_TASK_NAME}'",
        )

    if plat == "macos":
        if artifact.exists():
            if shutil.which("launchctl"):
                subprocess.run(["launchctl", "unload", str(artifact)], capture_output=True, timeout=10)
            artifact.unlink(missing_ok=True)
        return AutostartStatus(
            platform="macos",
            enabled=False,
            mechanism="macOS launchd LaunchAgent",
            artifact_path=str(artifact),
            detail="Unloaded and removed LaunchAgent",
        )

    if shutil.which("systemctl"):
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", LINUX_SERVICE_NAME],
            capture_output=True,
            timeout=10,
        )
    if artifact.exists():
        artifact.unlink(missing_ok=True)
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
    return AutostartStatus(
        platform="linux",
        enabled=False,
        mechanism="Linux systemd --user service",
        artifact_path=str(artifact),
        detail="Disabled and removed systemd --user unit",
    )
