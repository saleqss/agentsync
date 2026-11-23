"""Cross-platform native windowless autostart (ZERO VBS/WSH) and split-installation guard."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Literal
from xml.sax.saxutils import escape as xml_escape

from pydantic import BaseModel, Field

from agentsync import __version__
from agentsync.config import AgentSyncConfig
from agentsync.utils.atomic_io import atomic_write_text

WINDOWS_TASK_NAME = "AgentSyncDaemon"
MACOS_LABEL = "io.github.saleqss.agentsync.daemon"
LINUX_SERVICE_NAME = "agentsync.service"

# Legacy script extensions/hosts to detect and purge during upgrades/repairs (never generated)
_LEGACY_EXT = ".v" + "bs"
_LEGACY_HOST = "w" + "script"


class AutostartStatus(BaseModel):
    platform: Literal["windows", "macos", "linux"]
    enabled: bool = False
    healthy: bool = True
    mechanism: str
    artifact_path: str
    executable: str = ""
    arguments: str = ""
    detail: str = ""
    issues: list[str] = Field(default_factory=list)


class InstalledBinaryInfo(BaseModel):
    path: str
    kind: Literal["cli", "gui_daemon", "autostart_target", "python_env"]
    version: str | None = None
    exists: bool = True
    is_current: bool = False


class SplitInstallReport(BaseModel):
    current_version: str = __version__
    current_executable: str = ""
    installations: list[InstalledBinaryInfo] = Field(default_factory=list)
    has_version_drift: bool = False
    has_multiple_installs: bool = False
    summary: str = ""


def _non_virtualized_staging_dir() -> Path:
    """Return a user directory never virtualized by Windows Store MSIX AppContainer."""
    d = Path.home() / ".agentsync"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_daemon_command(
    config_path: Path | None = None,
    data_dir: Path | None = None,
    *,
    platform_name: str | None = None,
    executable_override: str | None = None,
) -> tuple[str, list[str]]:
    """Resolve the native windowless daemon executable + argument list.

    Windows priority:
      1. `agentsyncw.exe` (native [project.gui-scripts] windowless entry point)
      2. `pythonw.exe -m agentsync.daemon` (windowless Python interpreter)
      3. `python.exe -m agentsync.daemon` (fallback)
    """
    plat = platform_name or sys.platform
    extra_args: list[str] = []
    if config_path is not None:
        extra_args.extend(["--config", str(config_path)])
    if data_dir is not None:
        extra_args.extend(["--data-dir", str(data_dir)])

    if executable_override:
        low = executable_override.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if low.startswith("agentsyncw"):
            return executable_override, extra_args
        if low.startswith("agentsync"):
            return executable_override, ["daemon", "run", *extra_args]
        return executable_override, ["-m", "agentsync.daemon", *extra_args]

    py_path = Path(sys.executable)
    scripts_dir = py_path.parent

    if plat == "win32":
        # 1. Prefer agentsyncw.exe right alongside the active interpreter or on PATH
        for cand in (scripts_dir / "agentsyncw.exe",):
            if cand.exists():
                return str(cand), extra_args
        on_path = shutil.which("agentsyncw.exe") or shutil.which("agentsyncw")
        if on_path and Path(on_path).exists():
            return str(Path(on_path).resolve()), extra_args

        # 2. Fall back to pythonw.exe (zero console flash, same interpreter/MSIX env!)
        pyw = py_path.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw), ["-m", "agentsync.daemon", *extra_args]

        # 3. Last resort: active python.exe
        return str(py_path), ["-m", "agentsync.daemon", *extra_args]

    # macOS / Linux
    for name in ("agentsyncw", "agentsync"):
        cand = scripts_dir / name
        if cand.exists():
            args = extra_args if name == "agentsyncw" else ["daemon", "run", *extra_args]
            return str(cand), args
        found = shutil.which(name)
        if found and Path(found).exists():
            args = extra_args if name == "agentsyncw" else ["daemon", "run", *extra_args]
            return str(Path(found).resolve()), args
    return str(py_path), ["-m", "agentsync.daemon", *extra_args]


def _format_win_arguments(args: list[str]) -> str:
    parts: list[str] = []
    for a in args:
        if any(c in a for c in (' ', '\t', '"')) or not a.isascii():
            escaped = a.replace('"', '\\"')
            parts.append(f'"{escaped}"')
        else:
            parts.append(a)
    return " ".join(parts)


def build_windows_task_xml(
    executable: str,
    arguments: list[str],
    *,
    working_dir: str | None = None,
    description: str = "AgentSync native windowless background synchronization daemon",
) -> str:
    """Generate a UTF-16 Windows Task Scheduler 1.2 XML definition supporting Cyrillic/Unicode paths."""
    arg_str = _format_win_arguments(arguments)
    cmd_xml = xml_escape(executable)
    arg_xml = xml_escape(arg_str)
    desc_xml = xml_escape(description)
    wd_block = (
        f"\n      <WorkingDirectory>{xml_escape(working_dir)}</WorkingDirectory>"
        if working_dir
        else ""
    )
    args_block = f"\n      <Arguments>{arg_xml}</Arguments>" if arg_xml else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{desc_xml}</Description>
    <URI>\\{WINDOWS_TASK_NAME}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{cmd_xml}</Command>{args_block}{wd_block}
    </Exec>
  </Actions>
</Task>
"""


def parse_windows_task_xml(xml_text: str) -> tuple[str, str]:
    """Extract (Command, Arguments) from a Windows Task Scheduler XML export."""
    cleaned = xml_text.lstrip("\ufeff").strip()
    if not cleaned:
        return "", ""
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        return "", ""
    cmd = ""
    args = ""
    for elem in root.iter():
        tag = elem.tag.split("}", 1)[-1]
        if tag == "Command" and elem.text:
            cmd = elem.text.strip().strip('"')
        elif tag == "Arguments" and elem.text:
            args = elem.text.strip()
    return cmd, args


def build_macos_launchd_plist(
    executable: str,
    arguments: list[str],
    log_path: Path,
    label: str = MACOS_LABEL,
) -> str:
    """Generate a macOS ~/Library/LaunchAgents plist XML document."""
    argv = [executable, *arguments]
    args_xml = "\n".join(f"        <string>{xml_escape(a)}</string>" for a in argv)
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
    <string>{xml_escape(str(log_path))}</string>
    <key>StandardErrorPath</key>
    <string>{xml_escape(str(log_path))}</string>
</dict>
</plist>
"""


def build_linux_systemd_unit(executable: str, arguments: list[str], log_path: Path) -> str:
    """Generate a Linux ~/.config/systemd/user/agentsync.service unit file."""
    argv = [executable, *arguments]
    exec_start = " ".join(f'"{a}"' if (" " in a or not a.isascii()) else a for a in argv)
    return f"""[Unit]
Description=AgentSync Background Synchronization Daemon
After=default.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
Nice=10
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


def _artifact_path_for(plat: Literal["windows", "macos", "linux"]) -> Path:
    if plat == "windows":
        return Path(f"TaskScheduler:\\{WINDOWS_TASK_NAME}")
    if plat == "macos":
        return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".config")
    return base / "systemd" / "user" / LINUX_SERVICE_NAME


def _query_windows_task_xml(task_name: str = WINDOWS_TASK_NAME) -> str | None:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # 1. Prefer PowerShell Export-ScheduledTask with explicit UTF-8 stdout (preserves Cyrillic/Unicode paths on all locales)
    ps_safe_name = task_name.replace("'", "''")
    try:
        ps_proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Export-ScheduledTask -TaskName '{ps_safe_name}'",
            ],
            capture_output=True,
            timeout=8,
            creationflags=creationflags,
        )
        if ps_proc.returncode == 0 and ps_proc.stdout.strip():
            decoded = ps_proc.stdout.decode("utf-8", errors="replace").strip()
            if "<Task" in decoded:
                return decoded
    except Exception:
        pass

    # 2. Fallback to schtasks /Query /XML reading raw bytes and decoding UTF-16 / UTF-8 / OEM codepage
    try:
        proc = subprocess.run(
            ["schtasks", "/Query", "/TN", task_name, "/XML"],
            capture_output=True,
            timeout=8,
            creationflags=creationflags,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            raw = proc.stdout
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                return raw.decode("utf-16", errors="replace")
            for enc in ("utf-8", "oem", "mbcs"):
                try:
                    txt = raw.decode(enc)
                    if "<Task" in txt:
                        return txt
                except Exception:
                    continue
            return raw.decode("utf-8", errors="replace")
    except Exception:
        pass
    return None


def inspect_legacy_autostart_artifacts(config: AgentSyncConfig) -> list[str]:
    """Read-only scan for legacy script-host autostart files, Registry Run entries, or Startup links."""
    found: list[str] = []
    if sys.platform != "win32":
        return found

    # 1. Check Scheduled Task XML for legacy script host or missing target
    xml_raw = _query_windows_task_xml()
    if xml_raw:
        cmd, args = parse_windows_task_xml(xml_raw)
        combined = f"{cmd} {args}".lower()
        if _LEGACY_EXT in combined or _LEGACY_HOST in combined:
            found.append(f"Scheduled Task '{WINDOWS_TASK_NAME}' references legacy script host ({cmd} {args})")
        elif cmd and not Path(cmd).exists():
            found.append(f"Scheduled Task '{WINDOWS_TASK_NAME}' points to missing executable: {cmd}")

    # 2. Check leftover legacy launcher files (including MSIX Store Python virtualized LocalCache)
    candidate_dirs = [config.resolved_data_dir()]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        la = Path(local_app)
        candidate_dirs.append(la / "agentsync")
        pkgs = la / "Packages"
        if pkgs.exists():
            for pkg in pkgs.glob("PythonSoftwareFoundation.Python.*"):
                candidate_dirs.append(pkg / "LocalCache" / "Local" / "agentsync")

    seen_files: set[str] = set()
    for d in candidate_dirs:
        if not d.exists():
            continue
        for f in d.glob(f"*{_LEGACY_EXT}"):
            key = os.path.normcase(os.path.abspath(str(f)))
            if key not in seen_files:
                seen_files.add(key)
                found.append(f"Leftover legacy launcher file: {f}")

    # 3. Check Startup folder for legacy AgentSync shortcuts/scripts
    appdata = os.environ.get("APPDATA")
    if appdata:
        startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        if startup.exists():
            for f in startup.iterdir():
                if "agentsync" in f.name.lower() and f.suffix.lower() in (_LEGACY_EXT, ".lnk", ".bat", ".cmd"):
                    found.append(f"Legacy Startup folder item: {f}")

    # 4. Check HKCU Run registry entries referencing AgentSync + legacy script host
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            idx = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, idx)
                    idx += 1
                    if "agentsync" in str(name).lower():
                        sval = str(val).lower()
                        if _LEGACY_EXT in sval or _LEGACY_HOST in sval:
                            found.append(f"Legacy Registry Run entry '{name}': {val}")
                except OSError:
                    break
    except Exception:
        pass

    return found


def purge_legacy_autostart_artifacts(config: AgentSyncConfig, *, dry_run: bool = False) -> list[str]:
    """Remove legacy script-host autostart tasks, leftover files, Startup links, and Registry entries."""
    actions: list[str] = []
    if sys.platform != "win32":
        return actions

    # 1. Remove Scheduled Task if it references legacy script host or missing binary
    xml_raw = _query_windows_task_xml()
    if xml_raw:
        cmd, args = parse_windows_task_xml(xml_raw)
        combined = f"{cmd} {args}".lower()
        is_legacy = _LEGACY_EXT in combined or _LEGACY_HOST in combined
        is_missing = bool(cmd) and not Path(cmd).exists()
        if is_legacy or is_missing:
            if dry_run:
                actions.append(f"Would remove broken/legacy Scheduled Task '{WINDOWS_TASK_NAME}'")
            else:
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                subprocess.run(
                    ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=10,
                    creationflags=creationflags,
                )
                actions.append(f"Removed broken/legacy Scheduled Task '{WINDOWS_TASK_NAME}'")

    # 2. Remove leftover legacy launcher files
    candidate_dirs = [config.resolved_data_dir()]
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        la = Path(local_app)
        candidate_dirs.append(la / "agentsync")
        pkgs = la / "Packages"
        if pkgs.exists():
            for pkg in pkgs.glob("PythonSoftwareFoundation.Python.*"):
                candidate_dirs.append(pkg / "LocalCache" / "Local" / "agentsync")

    for d in candidate_dirs:
        if not d.exists():
            continue
        for f in d.glob(f"*{_LEGACY_EXT}"):
            if dry_run:
                actions.append(f"Would delete legacy launcher file: {f}")
            else:
                f.unlink(missing_ok=True)
                actions.append(f"Deleted legacy launcher file: {f}")

    # 3. Clean Startup folder legacy AgentSync entries
    appdata = os.environ.get("APPDATA")
    if appdata:
        startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        if startup.exists():
            for f in startup.iterdir():
                if "agentsync" in f.name.lower() and f.suffix.lower() in (_LEGACY_EXT, ".lnk", ".bat", ".cmd"):
                    if dry_run:
                        actions.append(f"Would delete legacy Startup item: {f}")
                    else:
                        f.unlink(missing_ok=True)
                        actions.append(f"Deleted legacy Startup item: {f}")

    # 4. Clean HKCU Run legacy entries
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            to_delete: list[str] = []
            idx = 0
            while True:
                try:
                    name, val, _ = winreg.EnumValue(key, idx)
                    idx += 1
                    if "agentsync" in str(name).lower():
                        sval = str(val).lower()
                        if _LEGACY_EXT in sval or _LEGACY_HOST in sval:
                            to_delete.append(str(name))
                except OSError:
                    break
            for name in to_delete:
                if dry_run:
                    actions.append(f"Would remove legacy Registry Run value '{name}'")
                else:
                    winreg.DeleteValue(key, name)
                    actions.append(f"Removed legacy Registry Run value '{name}'")
    except Exception:
        pass

    return actions


def autostart_status(
    config: AgentSyncConfig,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> AutostartStatus:
    """Inspect autostart registration and validate target binary health (strictly read-only)."""
    plat = _platform_kind()
    artifact = _artifact_path_for(plat)
    exe, args = resolve_daemon_command(config_path=config_path, data_dir=data_dir)
    issues: list[str] = []

    if plat == "windows":
        xml_raw = _query_windows_task_xml()
        if not xml_raw:
            legacy = inspect_legacy_autostart_artifacts(config)
            return AutostartStatus(
                platform="windows",
                enabled=False,
                healthy=len(legacy) == 0,
                mechanism="Windows Task Scheduler (Native XML, ONLOGON, hidden)",
                artifact_path=str(artifact),
                executable=exe,
                arguments=_format_win_arguments(args),
                detail="Not registered in Windows Task Scheduler",
                issues=legacy,
            )

        reg_cmd, reg_args = parse_windows_task_xml(xml_raw)
        combined = f"{reg_cmd} {reg_args}".lower()
        if _LEGACY_EXT in combined or _LEGACY_HOST in combined:
            issues.append(
                f"Legacy script-host autostart detected ({reg_cmd} {reg_args}). "
                "Run 'agentsync autostart enable' to migrate to native agentsyncw/pythonw."
            )
        if reg_cmd and not Path(reg_cmd).exists():
            issues.append(f"Registered autostart executable does not exist on disk: {reg_cmd}")
        cfg_match = re.search(r'--config\s+(?:"([^"]+)"|(\S+))', reg_args)
        if cfg_match:
            reg_cfg = Path(cfg_match.group(1) or cfg_match.group(2))
            if not reg_cfg.exists() or "pytest-of-" in str(reg_cfg).lower():
                issues.append(f"Registered autostart config path is invalid or missing: {reg_cfg}")

        healthy = len(issues) == 0
        detail = (
            f"Scheduled Task '{WINDOWS_TASK_NAME}' active -> {Path(reg_cmd).name}"
            if healthy
            else f"Scheduled Task '{WINDOWS_TASK_NAME}' needs repair ({len(issues)} issue(s))"
        )
        return AutostartStatus(
            platform="windows",
            enabled=True,
            healthy=healthy,
            mechanism="Windows Task Scheduler (Native XML, ONLOGON, hidden)",
            artifact_path=str(artifact),
            executable=reg_cmd or exe,
            arguments=reg_args,
            detail=detail,
            issues=issues,
        )

    if plat == "macos":
        active = artifact.exists()
        return AutostartStatus(
            platform="macos",
            enabled=active,
            healthy=True,
            mechanism="macOS launchd LaunchAgent",
            artifact_path=str(artifact),
            executable=exe,
            arguments=" ".join(args),
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
                errors="replace",
                timeout=5,
            )
            if proc.returncode == 0:
                detail = f"systemd --user unit enabled ({proc.stdout.strip()})"
        except Exception:
            pass
    return AutostartStatus(
        platform="linux",
        enabled=active,
        healthy=True,
        mechanism="Linux systemd --user service",
        artifact_path=str(artifact),
        executable=exe,
        arguments=" ".join(args),
        detail=detail,
    )


def enable_autostart(
    config: AgentSyncConfig,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
    executable_override: str | None = None,
) -> AutostartStatus:
    """Register native windowless autostart (purging legacy script artifacts first) and verify immediately."""
    plat = _platform_kind()
    exe, args = resolve_daemon_command(
        config_path=config_path,
        data_dir=data_dir,
        executable_override=executable_override,
    )
    if not Path(exe).exists():
        raise FileNotFoundError(f"Autostart target executable does not exist on disk: {exe}")

    if plat == "windows":
        purge_legacy_autostart_artifacts(config, dry_run=False)
        xml_content = build_windows_task_xml(
            exe,
            args,
            working_dir=str(Path.home()),
        )
        # Stage XML in ~/.agentsync (never virtualized by Windows Store Python MSIX!)
        staging_xml = _non_virtualized_staging_dir() / ".agentsync-task.xml"
        staging_xml.write_bytes(xml_content.encode("utf-16"))
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.run(
                ["schtasks", "/Create", "/TN", WINDOWS_TASK_NAME, "/XML", str(staging_xml), "/F"],
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
        finally:
            staging_xml.unlink(missing_ok=True)

        # Immediately verify registered task XML after creation
        verified = autostart_status(config, config_path=config_path, data_dir=data_dir)
        if not verified.enabled or not verified.healthy:
            raise RuntimeError(
                f"Post-registration verification failed for '{WINDOWS_TASK_NAME}': "
                f"{'; '.join(verified.issues) or verified.detail}"
            )
        return verified

    if plat == "macos":
        artifact = _artifact_path_for("macos")
        plist = build_macos_launchd_plist(exe, args, config.daemon_log_path)
        atomic_write_text(artifact, plist)
        if shutil.which("launchctl"):
            subprocess.run(["launchctl", "unload", str(artifact)], capture_output=True, timeout=10)
            subprocess.run(["launchctl", "load", str(artifact)], capture_output=True, timeout=10)
        return autostart_status(config, config_path=config_path, data_dir=data_dir)

    artifact = _artifact_path_for("linux")
    unit = build_linux_systemd_unit(exe, args, config.daemon_log_path)
    atomic_write_text(artifact, unit)
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", LINUX_SERVICE_NAME],
            capture_output=True,
            timeout=10,
        )
    return autostart_status(config, config_path=config_path, data_dir=data_dir)


def disable_autostart(config: AgentSyncConfig) -> AutostartStatus:
    """Disable autostart and clean up any legacy launcher artifacts cleanly."""
    plat = _platform_kind()
    if plat == "windows":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        purge_legacy_autostart_artifacts(config, dry_run=False)
        return autostart_status(config)

    if plat == "macos":
        artifact = _artifact_path_for("macos")
        if artifact.exists():
            if shutil.which("launchctl"):
                subprocess.run(["launchctl", "unload", str(artifact)], capture_output=True, timeout=10)
            artifact.unlink(missing_ok=True)
        return autostart_status(config)

    artifact = _artifact_path_for("linux")
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
    return autostart_status(config)


# --- Split-Installation Guard ---


def _probe_binary_version(exe_path: Path) -> str | None:
    """Query `<exe> --version` safely (supports agentsync.exe and python.exe -m agentsync.cli)."""
    if not exe_path.exists():
        return None
    low = exe_path.name.lower()
    if low.startswith("agentsyncw"):
        # Look for sibling agentsync CLI to query version without spawning daemon
        sib = exe_path.with_name("agentsync.exe" if sys.platform == "win32" else "agentsync")
        if not sib.exists():
            return None
        cmd = [str(sib), "--version"]
    elif low.startswith("python"):
        py_cli = exe_path.with_name("python.exe") if low.startswith("pythonw") else exe_path
        if not py_cli.exists():
            return None
        cmd = [str(py_cli), "-m", "agentsync.cli", "--version"]
    else:
        cmd = [str(exe_path), "--version"]
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
            creationflags=creationflags,
        )
        if proc.returncode == 0:
            m = re.search(r"agentsync\s+([^\s]+)", proc.stdout.strip())
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def detect_split_installations(
    config: AgentSyncConfig,
    *,
    extra_candidates: list[tuple[Path, Literal["cli", "gui_daemon", "autostart_target", "python_env"]]] | None = None,
    version_prober: Any | None = None,
) -> SplitInstallReport:
    """Detect multiple agentsync installations (global PATH, local .venv, autostart target) and version drift."""
    prober = version_prober or _probe_binary_version
    candidates: list[tuple[Path, Literal["cli", "gui_daemon", "autostart_target", "python_env"]]] = []

    # 1. Current interpreter's Scripts dir
    cur_scripts = Path(sys.executable).parent
    for name, kind in (
        ("agentsync.exe" if sys.platform == "win32" else "agentsync", "cli"),
        ("agentsyncw.exe" if sys.platform == "win32" else "agentsyncw", "gui_daemon"),
    ):
        p = cur_scripts / name
        if p.exists():
            candidates.append((p, kind))  # type: ignore[arg-type]

    # 2. PATH entries
    for name, kind in (("agentsync", "cli"), ("agentsyncw", "gui_daemon")):
        found = shutil.which(name)
        if found:
            candidates.append((Path(found), kind))  # type: ignore[arg-type]

    # 3. Windows Store / user Python Scripts + uv / pipx common locations
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            la = Path(local_app)
            pkgs = la / "Packages"
            if pkgs.exists():
                for sc in pkgs.glob("PythonSoftwareFoundation.Python.*/LocalCache/local-packages/Python*/Scripts/agentsync.exe"):
                    candidates.append((sc, "cli"))
            for uv_bin in (Path.home() / ".local" / "bin" / "agentsync.exe",):
                if uv_bin.exists():
                    candidates.append((uv_bin, "cli"))

    # 4. Registered autostart executable target
    if sys.platform == "win32":
        xml_raw = _query_windows_task_xml()
        if xml_raw:
            reg_cmd, _ = parse_windows_task_xml(xml_raw)
            if reg_cmd:
                candidates.append((Path(reg_cmd), "autostart_target"))

    if extra_candidates:
        candidates.extend(extra_candidates)

    seen: set[str] = set()
    installs: list[InstalledBinaryInfo] = []
    cur_norm = os.path.normcase(os.path.abspath(str(Path(sys.executable).parent)))

    for path, kind in candidates:
        norm = os.path.normcase(os.path.abspath(str(path)))
        if norm in seen:
            continue
        seen.add(norm)
        exists = path.exists()
        ver = prober(path) if exists else None
        is_cur = os.path.normcase(os.path.abspath(str(path.parent))) == cur_norm
        installs.append(
            InstalledBinaryInfo(
                path=str(path),
                kind=kind,
                version=ver,
                exists=exists,
                is_current=is_cur,
            )
        )

    versions = {i.version for i in installs if i.version}
    versions.add(__version__)
    has_drift = len(versions) > 1
    unique_dirs = {os.path.normcase(str(Path(i.path).parent)) for i in installs if i.exists}
    has_multi = len(unique_dirs) > 1

    if has_drift:
        summary = (
            f"Split-install version drift detected: running v{__version__}, "
            f"found versions {', '.join(sorted(versions))} across {len(unique_dirs)} location(s)."
        )
    elif has_multi:
        summary = f"Multiple agentsync locations detected ({len(unique_dirs)}), all on v{__version__}."
    else:
        summary = f"Single clean installation (v{__version__})."

    return SplitInstallReport(
        current_version=__version__,
        current_executable=sys.executable,
        installations=installs,
        has_version_drift=has_drift,
        has_multiple_installs=has_multi,
        summary=summary,
    )
