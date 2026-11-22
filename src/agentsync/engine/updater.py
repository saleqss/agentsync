"""Built-in self-updater with 12-hour cached GitHub release checks and install-method detection."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agentsync import __version__
from agentsync.utils.atomic_io import atomic_write_json

REPO_SLUG = "saleqss/agentsync"
REPO_GIT_URL = f"git+https://github.com/{REPO_SLUG}.git"
RELEASES_LATEST_URL = f"https://api.github.com/repos/{REPO_SLUG}/releases/latest"
RELEASES_ALL_URL = f"https://api.github.com/repos/{REPO_SLUG}/releases?per_page=10"
RAW_PYPROJECT_URL = f"https://raw.githubusercontent.com/{REPO_SLUG}/main/pyproject.toml"
CACHE_TTL_HOURS = 12

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
    error: str | None = None


class UpdateApplyResult(BaseModel):
    success: bool
    previous_version: str
    new_version: str | None = None
    install_method: InstallMethod
    command: list[str]
    output: str = ""
    restart_notice: str | None = None
    error: str | None = None


def parse_version_tuple(raw: str) -> tuple[int, int, int, int, int]:
    """Parse a semantic version string like `v0.4.0` or `0.5.0-rc1` into a comparable 5-tuple."""
    cleaned = raw.strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]?(a|alpha|b|beta|rc|dev)\.?(\d*))?$", cleaned, re.IGNORECASE)
    if not m:
        nums = [int(x) for x in re.findall(r"\d+", cleaned)[:3]]
        while len(nums) < 3:
            nums.append(0)
        return (nums[0], nums[1], nums[2], 3, 0)
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

    # Editable checkout: package file sits inside `<repo>/src/agentsync/...` with sibling `pyproject.toml`
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


def check_for_updates(
    cache_path: Path | None = None,
    *,
    current_version: str = __version__,
    force: bool = False,
    include_pre: bool = False,
    fetcher: Callable[[str], str] | None = None,
) -> UpdateCheckResult:
    """Check GitHub Releases (with raw pyproject.toml fallback) using a 12-hour non-blocking cache."""
    method = detect_install_method()
    now = datetime.now(UTC)

    # 1. Serve fresh cache when available and not forced
    if not force and cache_path is not None and cache_path.exists():
        try:
            cached = UpdateCheckResult.model_validate_json(cache_path.read_text(encoding="utf-8"))
            if cached.checked_at_utc:
                checked_dt = datetime.fromisoformat(cached.checked_at_utc.replace("Z", "+00:00"))
                if now - checked_dt < timedelta(hours=CACHE_TTL_HOURS) and not cached.error:
                    return cached.model_copy(
                        update={
                            "current_version": current_version,
                            "update_available": is_newer_version(cached.latest_version, current_version),
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

    # 2. Query GitHub Releases API
    try:
        url = RELEASES_ALL_URL if include_pre else RELEASES_LATEST_URL
        raw = get_fn(url)
        data = json.loads(raw)
        rel = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        tag = str(rel.get("tag_name") or "").strip().lstrip("vV")
        if tag:
            latest_ver = tag
            release_url = str(rel.get("html_url") or release_url)
            release_notes = str(rel.get("body") or "")[:600]
    except Exception as exc:
        err_msg = str(exc)

    # 3. Fallback to raw pyproject.toml on main branch if Releases API returned 404 or empty
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
    result = UpdateCheckResult(
        current_version=current_version,
        latest_version=final_latest,
        update_available=bool(latest_ver and is_newer_version(final_latest, current_version)),
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
) -> list[str]:
    """Return the exact CLI upgrade command matching the detected installation method."""
    if method == "uv" and shutil.which("uv"):
        cmd = ["uv", "tool", "install", "--force", REPO_GIT_URL]
        if include_pre:
            cmd.insert(3, "--prerelease=allow")
        return cmd
    if method == "pipx" and shutil.which("pipx"):
        return ["pipx", "install", "--force", REPO_GIT_URL]
    if method == "editable":
        target = str(repo_root) if repo_root else REPO_GIT_URL
        return [sys.executable, "-m", "pip", "install", "--upgrade", "-e", target]
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if include_pre:
        cmd.append("--pre")
    cmd.append(REPO_GIT_URL)
    return cmd


def perform_update(
    *,
    method: InstallMethod | None = None,
    include_pre: bool = False,
    runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> UpdateApplyResult:
    """Execute an in-place upgrade of agentsync and verify the resulting version."""
    chosen = method or detect_install_method()
    repo_root: Path | None = None
    if chosen == "editable":
        for p in Path(__file__).resolve().parents:
            if (p / "pyproject.toml").exists():
                repo_root = p
                break

    cmd = build_upgrade_command(chosen, include_pre=include_pre, repo_root=repo_root)

    def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, capture_output=True, text=True, timeout=180)

    exec_fn = runner or _run
    try:
        proc = exec_fn(cmd)
    except Exception as exc:
        return UpdateApplyResult(
            success=False,
            previous_version=__version__,
            install_method=chosen,
            command=cmd,
            error=str(exc),
        )

    combined = "\n".join(p for p in (proc.stdout.strip(), proc.stderr.strip()) if p)
    if proc.returncode != 0:
        notice = None
        if sys.platform == "win32" and ("access is denied" in combined.lower() or "being used by another process" in combined.lower()):
            notice = (
                "Windows holds a lock on agentsync.exe while running. "
                "Close open agentsync terminals and re-run the upgrade command."
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
