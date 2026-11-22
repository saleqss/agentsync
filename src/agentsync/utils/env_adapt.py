"""Adaptive OS, shell, WSL2, terminal capability, vendored binary, and filesystem link utilities."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agentsync.utils.atomic_io import remove_tree_if_exists, sync_directory_files

SkillLinkMode = Literal["auto", "symlink", "copy"]


class EnvironmentProfile(BaseModel):
    """Detected runtime OS, shell, container/WSL status, and terminal capabilities."""

    os_name: str
    os_version: str
    is_windows: bool
    is_macos: bool
    is_linux: bool
    is_wsl: bool
    is_ssh: bool
    is_docker: bool
    is_ci: bool
    shell: str
    is_tty: bool
    columns: int
    rows: int
    no_color: bool
    utf8_capable: bool
    legacy_console: bool = False
    tui_recommended: bool

    def badge(self) -> str:
        env = "WSL2" if self.is_wsl else ("Windows" if self.is_windows else ("macOS" if self.is_macos else "Linux"))
        extras = []
        if self.is_ssh:
            extras.append("SSH")
        if self.is_docker:
            extras.append("Docker")
        if self.legacy_console:
            extras.append("conhost")
        if extras:
            return f"{env}/{'+'.join(extras)} ({self.shell})"
        return f"{env} ({self.shell})"


def is_legacy_windows_console(env_vars: dict[str, str] | None = None, platform_name: str | None = None) -> bool:
    """Return True when running inside legacy Windows conhost.exe (PowerShell 5.1 / cmd.exe without Windows Terminal)."""
    plat = platform_name if platform_name is not None else sys.platform
    if plat != "win32":
        return False
    env = env_vars if env_vars is not None else dict(os.environ)
    if env.get("AGENTSYNC_ASCII") in ("1", "true", "yes"):
        return True
    modern_markers = ("WT_SESSION", "TERM_PROGRAM", "ConEmuPID", "ANSICON", "ALACRITTY_LOG", "WEZTERM_PANE")
    return not any(env.get(k) for k in modern_markers)


def enforce_utf8_io() -> None:
    """Ensure stdout/stderr never crash with UnicodeEncodeError on Windows legacy consoles."""
    if sys.platform == "win32":
        os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                try:
                    stream.reconfigure(errors="replace")
                except Exception:
                    pass


def is_wsl_environment() -> bool:
    """Detect whether running inside Windows Subsystem for Linux (WSL1/WSL2)."""
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        rel = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
        return "microsoft" in rel or "wsl" in rel
    except OSError:
        return False


def wsl_to_windows_path(path_str: str) -> str:
    """Convert `/mnt/<drive>/...` to `<DRIVE>:\\...` when bridging WSL2 and Windows desktop apps."""
    m = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", path_str.strip())
    if not m:
        return path_str
    drive = m.group(1).upper()
    rest = (m.group(2) or "").replace("/", "\\")
    return f"{drive}:\\{rest}" if rest else f"{drive}:\\"


def windows_to_wsl_path(path_str: str) -> str:
    """Convert `C:\\...` or `C:/...` to `/mnt/c/...` for Linux CLI agents inside WSL2."""
    m = re.match(r"^([a-zA-Z]):[\\/](.*)$", path_str.strip())
    if not m:
        return path_str
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}".rstrip("/")


def detect_shell() -> str:
    """Identify the active shell across Windows, macOS, and Linux."""
    if sys.platform == "win32":
        if os.environ.get("PSModulePath"):
            return "pwsh" if "powershell\\7" in os.environ.get("PSModulePath", "").lower() else "powershell"
        comspec = os.environ.get("COMSPEC", "")
        if "cmd.exe" in comspec.lower():
            return "cmd"
        return "win-shell"
    sh = os.environ.get("SHELL", "")
    return Path(sh).name if sh else "sh"


def detect_environment() -> EnvironmentProfile:
    """Probe current OS, WSL/SSH/Docker/CI status, and terminal rendering capabilities."""
    is_win = sys.platform == "win32"
    is_mac = sys.platform == "darwin"
    is_lin = sys.platform.startswith("linux")
    wsl = is_wsl_environment()
    ssh = bool(os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"))
    docker = Path("/.dockerenv").exists() or bool(os.environ.get("REMOTE_CONTAINERS"))
    ci = bool(
        os.environ.get("CI")
        or os.environ.get("GITHUB_ACTIONS")
        or os.environ.get("GITLAB_CI")
        or os.environ.get("JENKINS_URL")
    )
    is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)() and getattr(sys.stdout, "isatty", lambda: False)())
    term_size = shutil.get_terminal_size(fallback=(80, 24))
    no_color = bool(os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb")
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    legacy_con = is_legacy_windows_console()
    utf8 = ("utf" in enc or is_mac or is_lin or bool(os.environ.get("WT_SESSION"))) and not legacy_con

    tui_ok = is_tty and not ci and os.environ.get("TERM") != "dumb" and term_size.columns >= 60 and term_size.lines >= 18

    return EnvironmentProfile(
        os_name=platform.system(),
        os_version=platform.release(),
        is_windows=is_win,
        is_macos=is_mac,
        is_linux=is_lin,
        is_wsl=wsl,
        is_ssh=ssh,
        is_docker=docker,
        is_ci=ci,
        shell=detect_shell(),
        is_tty=is_tty,
        columns=term_size.columns,
        rows=term_size.lines,
        no_color=no_color,
        utf8_capable=utf8,
        legacy_console=legacy_con,
        tui_recommended=tui_ok,
    )


def resolve_vendored_binary(tool_name: str) -> str | None:
    """Resolve an AI tool's real executable even when vendored inside Electron/MSIX/app containers."""
    cli_candidates: dict[str, list[str]] = {
        "codex": ["codex", "codex.exe", "codex.cmd"],
        "claude-code": ["claude", "claude.exe", "claude.cmd"],
        "opencode": ["opencode", "opencode.exe"],
        "antigravity": ["antigravity", "Antigravity.exe"],
        "deepseek-harness": ["dsh", "dsh.exe", "DeepSeek Harness.exe"],
        "zed": ["zed", "zed.exe"],
        "gemini-cli": ["gemini", "gemini.cmd"],
        "cursor": ["cursor", "Cursor.exe"],
        "windsurf": ["windsurf", "Windsurf.exe"],
        "vscode-copilot": ["code", "code.cmd", "Code.exe"],
    }
    for cand in cli_candidates.get(tool_name, []):
        found = shutil.which(cand)
        if found:
            return found

    home = Path.home()
    local_app = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    prog_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")

    if sys.platform == "win32":
        explicit: dict[str, list[Path]] = {
            "antigravity": [local_app / "Programs" / "Antigravity" / "Antigravity.exe"],
            "deepseek-harness": [local_app / "Programs" / "DeepSeek Harness" / "DeepSeek Harness.exe"],
            "zed": [local_app / "Programs" / "Zed" / "Zed.exe", local_app / "Zed" / "Zed.exe"],
            "cursor": [local_app / "Programs" / "cursor" / "Cursor.exe"],
            "windsurf": [local_app / "Programs" / "Windsurf" / "Windsurf.exe"],
            "vscode-copilot": [local_app / "Programs" / "Microsoft VS Code" / "Code.exe"],
            "opencode": [local_app / "Programs" / "OpenCode" / "OpenCode.exe", home / ".opencode" / "bin" / "opencode.exe"],
            "claude-code": [home / ".local" / "bin" / "claude.exe", home / ".claude" / "local" / "claude.exe"],
            "codex": [home / ".codex" / "bin" / "codex.exe"],
        }
        for p in explicit.get(tool_name, []):
            if p.exists():
                return str(p)
        if tool_name == "codex":
            winapps = prog_files / "WindowsApps"
            if winapps.exists():
                try:
                    for d in sorted(winapps.glob("OpenAI.Codex_*"), reverse=True):
                        for exe_rel in ("app/ChatGPT.exe", "app/resources/codex.exe", "app/codex.exe"):
                            cand_p = d / exe_rel
                            if cand_p.exists():
                                return str(cand_p)
                except PermissionError:
                    pass
    elif sys.platform == "darwin":
        mac_apps: dict[str, list[Path]] = {
            "codex": [Path("/Applications/Codex.app/Contents/MacOS/Codex")],
            "antigravity": [Path("/Applications/Antigravity.app/Contents/MacOS/Antigravity")],
            "zed": [Path("/Applications/Zed.app/Contents/MacOS/zed")],
            "cursor": [Path("/Applications/Cursor.app/Contents/MacOS/Cursor")],
            "windsurf": [Path("/Applications/Windsurf.app/Contents/MacOS/Windsurf")],
            "vscode-copilot": [Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")],
        }
        for p in mac_apps.get(tool_name, []):
            if p.exists():
                return str(p)
    else:
        for extra_bin in (home / ".local" / "bin", Path("/usr/local/bin")):
            for cand in cli_candidates.get(tool_name, []):
                p = extra_bin / cand
                if p.exists():
                    return str(p)
    return None


def link_or_copy_skill_dir(
    source_dir: Path,
    target_dir: Path,
    files: dict[str, bytes],
    mode: SkillLinkMode = "auto",
) -> tuple[bool, Literal["symlink", "junction", "copy"]]:
    """Place a skill directory using the requested strategy with graceful Windows privilege fallback.

    - `copy`: Always performs atomic file sync (safest, completely portable).
    - `symlink` / `auto`: Attempts directory symlink when `source_dir` exists; on Windows `WinError 1314`
      (missing SeCreateSymbolicLinkPrivilege), falls back to an NTFS directory junction, and finally to
      an atomic real copy if linking fails.
    """
    if mode == "copy" or not source_dir.exists() or not source_dir.is_dir():
        if target_dir.is_symlink():
            target_dir.unlink()
        changed = sync_directory_files(target_dir, files)
        return changed, "copy"

    # Already points to the exact same resolved directory
    try:
        if target_dir.exists() and target_dir.resolve() == source_dir.resolve():
            return False, ("symlink" if target_dir.is_symlink() else "junction")
    except OSError:
        pass

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.is_symlink():
        target_dir.unlink()
    elif target_dir.exists():
        remove_tree_if_exists(target_dir)

    # Try native symlink first
    try:
        os.symlink(source_dir, target_dir, target_is_directory=True)
        return True, "symlink"
    except OSError:
        pass

    # On Windows, fall back to NTFS directory junction (requires no admin / Developer Mode!)
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target_dir), str(source_dir)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0 and target_dir.exists():
                return True, "junction"
        except Exception:
            pass

    # Final safe fallback: atomic real copy
    changed = sync_directory_files(target_dir, files)
    return changed, "copy"
