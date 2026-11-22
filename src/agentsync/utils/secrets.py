"""Secret detection and deterministic redaction for logs, diff reports, and CLI output."""

from __future__ import annotations

import hashlib
import re
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(?i)(api[_-]?key|token|secret|pass(word|wd)?|auth(orization)?|bearer|cookie|"
    r"credential|private[_-]?key|access[_-]?key|client[_-]?secret|webhook|signing|"
    r"connection[_-]?string|database[_-]?url|dsn|session[_-]?id|jwt|pat)"
)

_SECRET_VALUE_RE = re.compile(
    r"("
    r"sk-[A-Za-z0-9_\-]{12,}|"
    r"sk-proj-[A-Za-z0-9_\-]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"AIza[0-9A-Za-z_\-]{20,}|"
    r"xox[baprs]-[0-9A-Za-z\-]{10,}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}|"
    r"(?i:Bearer)\s+[A-Za-z0-9_\-\.=:/+]{8,}|"
    r"(?i:Basic)\s+[A-Za-z0-9+/=]{8,}"
    r")"
)

_URL_CREDS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+\-.]*://)([^/\s:@]+):([^/\s@]+)@")
_PLACEHOLDER_RE = re.compile(r"^\s*(\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*)\s*$")
_BEARER_PLACEHOLDER_RE = re.compile(r"(?i)^Bearer\s+\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def is_placeholder(value: str) -> bool:
    """Return True if value is a variable placeholder like `${TOKEN}` rather than a literal secret."""
    return bool(_PLACEHOLDER_RE.match(value) or _BEARER_PLACEHOLDER_RE.match(value.strip()))


def mask_secret_string(value: str) -> str:
    """Deterministically redact a secret string while preserving length and a short fingerprint."""
    if not value or is_placeholder(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"***REDACTED(len={len(value)},sha256={digest})***"


def is_secret_key(key: str) -> bool:
    """Return True if a config/env/header key name likely holds sensitive credentials."""
    return bool(_SECRET_KEY_RE.search(key))


def mask_text(text: str) -> str:
    """Redact inline tokens, Bearer/Basic headers, and URL credentials within free text."""
    if not text:
        return text
    out = _URL_CREDS_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:***REDACTED***@", text)
    out = _SECRET_VALUE_RE.sub(lambda m: mask_secret_string(m.group(0)), out)
    return out


def mask_mapping(data: Any, parent_key: str = "") -> Any:
    """Recursively redact secret values inside nested dicts/lists for safe logging or CLI output."""
    if isinstance(data, dict):
        return {k: mask_mapping(v, parent_key=str(k)) for k, v in data.items()}
    if isinstance(data, list):
        return [mask_mapping(item, parent_key=parent_key) for item in data]
    if isinstance(data, tuple):
        return tuple(mask_mapping(item, parent_key=parent_key) for item in data)
    if isinstance(data, str):
        if is_placeholder(data):
            return data
        if parent_key and is_secret_key(parent_key):
            return mask_secret_string(data)
        return mask_text(data)
    return data
