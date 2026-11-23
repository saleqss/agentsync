"""Cross-platform path normalization with strict Windows drive/slash/symlink handling."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

_WIN_DRIVE_RE = re.compile(r"^([a-zA-Z]):(.*)$")
_LEADING_SLASH_DRIVE_RE = re.compile(r"^/+([a-zA-Z]:(?:[/\\].*)?)$")
_WSL_MNT_RE = re.compile(r"^(?:win32:)?/mnt/([a-zA-Z])(/.*)?$", re.IGNORECASE)
_PERCENT_ENC_RE = re.compile(r"%[0-9a-fA-F]{2}")


def strip_win_extended_prefix(raw: str) -> str:
    """Strip Windows extended-length path prefixes (`\\\\?\\` and `\\\\?\\UNC\\`)."""
    s = raw.strip()
    if s.startswith("\\\\?\\UNC\\") or s.startswith("//?/UNC/"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\") or s.startswith("//?/"):
        return s[4:]
    return s


def file_uri_to_path(uri: str) -> str:
    """Convert a `file:///` URI (including percent-encoded `c%3A` and UTF-8) to a local path."""
    s = uri.strip()
    if s.lower().startswith("file/"):
        s = "file:///" + s[5:].lstrip("/")
    if not s.lower().startswith("file://"):
        if _PERCENT_ENC_RE.search(s):
            s = unquote(unquote(s))
        return s
    parsed = urlparse(s)
    raw_path = unquote(unquote(parsed.path))
    if parsed.netloc and parsed.netloc.lower() != "localhost":
        netloc = unquote(parsed.netloc)
        if _WIN_DRIVE_RE.match(netloc):
            return f"{netloc}{raw_path}"
        return f"//{netloc}{raw_path}".replace("/", "\\")
    m_lead = _LEADING_SLASH_DRIVE_RE.match(raw_path)
    if m_lead:
        raw_path = m_lead.group(1)
    return raw_path


def _resolve_with_existing_ancestor(path_str: str) -> str:
    """Resolve symlinks and Windows junctions via os.path.realpath even if trailing parts do not exist."""
    p = Path(path_str)
    if p.exists():
        return strip_win_extended_prefix(os.path.realpath(str(p)))

    tail_parts: list[str] = []
    cur = p
    while True:
        parent = cur.parent
        if parent == cur:
            return strip_win_extended_prefix(os.path.normpath(str(p)))
        tail_parts.append(cur.name)
        cur = parent
        if cur.exists():
            base = strip_win_extended_prefix(os.path.realpath(str(cur)))
            tail_parts.reverse()
            return os.path.normpath(os.path.join(base, *tail_parts))


def normalize_path(raw: str) -> str:
    """Return canonical forward-slash path with uppercase Windows drive letter and resolved symlinks.

    Examples:
        `\\\\?\\c:\\Users\\dev\\repo\\`       -> `C:/Users/dev/repo`
        `file:///c%3A/Users/dev/repo`        -> `C:/Users/dev/repo`
        `/home/dev/repo/`                    -> `/home/dev/repo`
    """
    if not raw or not raw.strip():
        return ""

    s = raw.strip()
    if s.lower().startswith("file://") or s.lower().startswith("file/"):
        s = file_uri_to_path(s)
    elif _PERCENT_ENC_RE.search(s) and ("c%3a" in s.lower() or "%2f" in s.lower() or "%5c" in s.lower() or "%d0%" in s.lower() or "%d1%" in s.lower()):
        s = unquote(unquote(s))
    s = strip_win_extended_prefix(s)
    m_wsl = _WSL_MNT_RE.match(s)
    if m_wsl:
        drive_letter = m_wsl.group(1).upper()
        rest_wsl = m_wsl.group(2) or "/"
        s = f"{drive_letter}:{rest_wsl}"
    m_lead = _LEADING_SLASH_DRIVE_RE.match(s)
    if m_lead:
        s = m_lead.group(1)

    is_windows_style = bool(_WIN_DRIVE_RE.match(s)) or s.startswith("\\\\") or s.startswith("//")
    if is_windows_style:
        s = s.replace("/", "\\")
        if os.name == "nt":
            s = os.path.normpath(s)
            s = _resolve_with_existing_ancestor(s)
        else:
            # Pure-string normalization for Windows paths tested on Linux/macOS CI
            parts = [p for p in s.split("\\") if p not in ("", ".")]
            resolved: list[str] = []
            for part in parts:
                if part == ".." and len(resolved) > 1:
                    resolved.pop()
                else:
                    resolved.append(part)
            prefix = "\\\\" if s.startswith("\\\\") else ""
            s = prefix + "\\".join(resolved)
    else:
        s = os.path.normpath(s)
        if os.path.exists(s):
            s = os.path.realpath(s)

    s = strip_win_extended_prefix(s).replace("\\", "/")
    if s.startswith("//"):
        s = "//" + re.sub(r"/+", "/", s[2:])
    else:
        s = re.sub(r"/+", "/", s)

    m = _WIN_DRIVE_RE.match(s)
    if m:
        drive = m.group(1).upper()
        rest = m.group(2)
        if not rest:
            return f"{drive}:/"
        if len(rest) > 1 and rest.endswith("/"):
            rest = rest.rstrip("/")
        return f"{drive}:{rest}"

    if len(s) > 1 and s.endswith("/"):
        s = s.rstrip("/")
    return s


def path_dedup_key(raw: str) -> str:
    """Return canonical deduplication key (case-insensitive for Windows paths)."""
    norm = normalize_path(raw)
    if not norm:
        return ""
    if _WIN_DRIVE_RE.match(norm) or norm.startswith("//"):
        return norm.lower()
    return norm


def to_native_path(raw: str) -> str:
    """Convert canonical path into Windows backslash form for Windows paths or POSIX form otherwise."""
    norm = normalize_path(raw)
    if not norm:
        return ""
    if _WIN_DRIVE_RE.match(norm) or norm.startswith("//"):
        return norm.replace("/", "\\")
    return norm


def canonical_windows_cwd(raw: str) -> str:
    """Return canonical Windows working directory path (`C:\\Users\\...`) without `\\\\?\\` prefix."""
    return to_native_path(raw)


def path_to_file_uri(raw: str, encode_drive_colon: bool = True) -> str:
    """Convert a local path to Antigravity's `file:///c%3A/...` URI format."""
    norm = normalize_path(raw)
    if not norm:
        return ""
    m = _WIN_DRIVE_RE.match(norm)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2)
        if not rest.startswith("/"):
            rest = "/" + rest
        encoded_rest = quote(rest, safe="/")
        colon = "%3A" if encode_drive_colon else ":"
        return f"file:///{drive}{colon}{encoded_rest}"
    if norm.startswith("//"):
        parts = norm[2:].split("/", 1)
        host = parts[0]
        rest = "/" + parts[1] if len(parts) > 1 else ""
        return f"file://{host}{quote(rest, safe='/')}"
    return f"file://{quote(norm, safe='/')}"
