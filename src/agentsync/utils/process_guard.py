"""Process lock guard to prevent corrupting live SQLite/LevelDB stores while agents are running."""

from __future__ import annotations

from collections.abc import Iterable

import psutil
from pydantic import BaseModel


class RunningAppProcess(BaseModel):
    app: str
    pid: int
    name: str
    exe: str = ""


class AppsRunningError(RuntimeError):
    """Raised when write operations are blocked because target AI apps are currently running."""

    def __init__(self, processes: list[RunningAppProcess]) -> None:
        self.processes = processes
        apps = sorted({p.app for p in processes})
        details = "; ".join(f"{p.app}:{p.name}(pid={p.pid})" for p in processes[:8])
        if len(processes) > 8:
            details += f" (+{len(processes) - 8} more)"
        super().__init__(
            f"Write blocked: running processes detected for {', '.join(apps)} [{details}]. "
            "Close the running applications first or pass --force to override."
        )


def classify_process(
    name: str,
    exe: str,
    cmdline: list[str] | None = None,
) -> str | None:
    """Classify whether an OS process belongs to a managed AI coding tool."""
    lname = (name or "").lower()
    lexe = (exe or "").replace("/", "\\").lower()
    lcmd = " ".join(cmdline or []).replace("/", "\\").lower()

    # Exclude always-on Windows sandbox elevation service (does not hold DB locks)
    if lname in {"codex-windows-sandbox-service.exe", "codex-windows-sandbox-setup.exe"}:
        return None

    # Google Antigravity (IDE, Electron helpers, language server, bundled node)
    if lname in {"antigravity.exe", "antigravity"}:
        return "antigravity"
    if "\\programs\\antigravity\\" in lexe or "/antigravity.app/" in lcmd or "\\antigravity\\bin\\" in lexe:
        return "antigravity"
    if lname in {"language_server.exe", "language_server", "language_server_windows_x64.exe"} and "antigravity" in (
        lexe + " " + lcmd
    ):
        return "antigravity"
    if lname in {"msedgewebview2.exe", "node.exe", "node"} and (
        "\\antigravity\\" in lexe or "\\antigravity\\" in lcmd or ".antigravity" in lcmd
    ):
        return "antigravity"

    # OpenAI Codex (MSIX ChatGPT.exe host, codex.exe, Electron/WebView/runtime/plugin extension-host, WSL CLI)
    if lname in {
        "codex.exe",
        "codex",
        "codex-computer-use.exe",
        "codex-computer-use-swift.exe",
        "codex-code-mode-host.exe",
        "codex-command-runner.exe",
        "codex-app-server.exe",
    }:
        return "codex"
    if "\\windowsapps\\openai.codex" in lexe or "\\windowsapps\\openai.chatgpt" in lexe:
        return "codex"
    if "\\openai\\codex\\" in lexe or "\\codex-runtimes\\" in lexe or "\\.codex\\" in lexe:
        return "codex"
    if lname == "chatgpt.exe":
        return "codex"
    if lname in {"extension-host.exe", "extension-host"} and (
        "\\.codex\\" in lexe or "openai-bundled" in lexe or "\\.codex\\" in lcmd
    ):
        return "codex"
    if lname in {"msedgewebview2.exe", "node.exe", "node"} and (
        "openai.codex" in lexe
        or "openai.codex" in lcmd
        or "\\.codex\\" in lexe
        or "\\.codex\\" in lcmd
        or "\\openai\\codex\\" in lcmd
    ):
        return "codex"
    if lname in {"wsl.exe", "wslhost.exe"} and "codex" in lcmd:
        return "codex"

    # Claude Code
    if lname in {"claude.exe", "claude"} and (
        "claude-code" in lcmd or ".claude" in lexe or lname in {"claude.exe", "claude"}
    ):
        if "anthropic" in lexe or "claude" in lexe or "claude" in lcmd:
            return "claude-code"

    # OpenCode
    if lname in {"opencode.exe", "opencode", "opencode-desktop.exe"}:
        return "opencode"
    if "ai.opencode.desktop" in lexe or "\\opencode\\" in lexe:
        return "opencode"

    # DeepSeek Harness
    if lname in {"deepseek harness.exe", "dsh.exe", "dsh"}:
        return "deepseek-harness"
    if "\\programs\\deepseek harness\\" in lexe:
        return "deepseek-harness"

    # Zed Editor
    if lname in {"zed.exe", "zed"} or "\\programs\\zed\\" in lexe or "/zed.app/" in lcmd:
        return "zed"

    # Cursor IDE
    if lname in {"cursor.exe", "cursor"} or "\\programs\\cursor\\" in lexe:
        return "cursor"

    # Windsurf IDE
    if lname in {"windsurf.exe", "windsurf"} or "\\programs\\windsurf\\" in lexe:
        return "windsurf"

    # Gemini CLI
    if lname in {"gemini.exe", "gemini"} and "gemini" in (lexe + " " + lcmd):
        return "gemini-cli"

    return None


def detect_running_apps(
    check_apps: Iterable[str] | None = None,
) -> list[RunningAppProcess]:
    """Scan active OS processes and return those belonging to `check_apps`."""
    wanted = set(check_apps) if check_apps is not None else None
    found: list[RunningAppProcess] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            info = proc.info
            app = classify_process(
                info.get("name") or "",
                info.get("exe") or "",
                info.get("cmdline") or [],
            )
            if app and (wanted is None or app in wanted):
                found.append(
                    RunningAppProcess(
                        app=app,
                        pid=int(info["pid"]),
                        name=info.get("name") or "",
                        exe=info.get("exe") or "",
                    )
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return found


def assert_apps_closed(
    force: bool = False,
    check_apps: Iterable[str] | None = None,
) -> list[RunningAppProcess]:
    """Raise AppsRunningError if any checked tool is running and `force` is False."""
    running = detect_running_apps(check_apps=check_apps)
    if running and not force:
        raise AppsRunningError(running)
    return running
