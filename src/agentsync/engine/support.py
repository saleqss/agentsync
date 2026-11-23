"""Privacy-safe anonymized diagnostics bundle generator (`agentsync support-bundle`)."""

from __future__ import annotations

import getpass
import json
import os
import platform
import re
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from agentsync import __version__
from agentsync.accel import runtime_engine_label
from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig
from agentsync.engine.doctor import DoctorEngine
from agentsync.utils.env_adapt import detect_environment
from agentsync.utils.secrets import mask_text

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_WIN_USER_PATH_RE = re.compile(r"([A-Za-z]:[\\/]+Users[\\/]+)([^\\/\s\"']+)", re.IGNORECASE)
_POSIX_USER_PATH_RE = re.compile(r"(/(?:home|Users)/)([^/\s\"']+)")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-+/=]{8,}", re.IGNORECASE)
_TOKEN_KEY_RE = re.compile(
    r"((?:api[_-]?key|token|secret|password|auth|credential)\s*[:=]\s*['\"]?)([^\s'\",;}]{6,})",
    re.IGNORECASE,
)


def anonymize_text(text: str, *, extra_usernames: list[str] | None = None) -> str:
    """Scrub secrets, emails, usernames, home paths, and IP addresses from diagnostic text."""
    out = mask_text(text)
    out = _BEARER_RE.sub(r"\1<REDACTED>", out)
    out = _TOKEN_KEY_RE.sub(r"\1<REDACTED>", out)
    out = _EMAIL_RE.sub("<EMAIL_REDACTED>", out)
    out = _WIN_USER_PATH_RE.sub(r"\1<USER>", out)
    out = _POSIX_USER_PATH_RE.sub(r"\1<USER>", out)
    out = _IPV4_RE.sub("<IP_REDACTED>", out)

    names: set[str] = set(extra_usernames or [])
    for cand in (os.environ.get("USERNAME"), os.environ.get("USER")):
        if cand and len(cand) >= 3:
            names.add(cand)
    try:
        u = getpass.getuser()
        if u and len(u) >= 3:
            names.add(u)
    except Exception:
        pass
    home_name = Path.home().name
    if home_name and len(home_name) >= 3:
        names.add(home_name)

    for uname in sorted(names, key=len, reverse=True):
        out = re.sub(re.escape(uname), "<USER>", out, flags=re.IGNORECASE)
    return out


def create_support_bundle(
    output_zip: Path,
    config: AgentSyncConfig,
    adapters: list[ToolAdapter],
    *,
    extra_usernames: list[str] | None = None,
) -> Path:
    """Create an anonymized bug-report zip with zero chat transcripts, secrets, emails, or usernames."""
    env = detect_environment()
    doc = DoctorEngine(config, adapters)
    issues = doc.diagnose()

    env_payload = {
        "agentsync_version": __version__,
        "runtime_engine": runtime_engine_label(config.runtime_engine),
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "os_name": env.os_name,
        "os_version": env.os_version,
        "is_wsl": env.is_wsl,
        "is_ssh": env.is_ssh,
        "is_docker": env.is_docker,
        "shell": env.shell,
        "columns": env.columns,
        "rows": env.rows,
        "utf8_capable": env.utf8_capable,
        "generated_at_utc": datetime.now(UTC).isoformat(),
    }

    doctor_payload = [
        {
            "tool": i.tool,
            "severity": i.severity,
            "code": i.code,
            "fixable": i.fixable,
            "path": anonymize_text(i.path, extra_usernames=extra_usernames),
            "message": anonymize_text(i.message, extra_usernames=extra_usernames),
        }
        for i in issues
    ]

    cfg_raw = json.dumps(config.model_dump(exclude_none=True), indent=2, ensure_ascii=False)
    cfg_redacted = anonymize_text(cfg_raw, extra_usernames=extra_usernames)

    daemon_tail = "(no daemon.log present)\n"
    if config.daemon_log_path.exists():
        lines = config.daemon_log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        daemon_tail = anonymize_text("\n".join(lines), extra_usernames=extra_usernames) + "\n"

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = output_zip.with_suffix(output_zip.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("environment.json", json.dumps(env_payload, indent=2) + "\n")
            zf.writestr("doctor.json", json.dumps(doctor_payload, indent=2) + "\n")
            zf.writestr("config_redacted.json", cfg_redacted + "\n")
            zf.writestr("daemon_tail.log", daemon_tail)
            zf.writestr(
                "PRIVACY_NOTICE.txt",
                "This support bundle intentionally excludes all chat transcripts, rollout JSONL files, "
                "API keys, bearer tokens, email addresses, IP addresses, and OS usernames.\n",
            )
        tmp_zip.replace(output_zip)
    except BaseException:
        tmp_zip.unlink(missing_ok=True)
        raise

    return output_zip
