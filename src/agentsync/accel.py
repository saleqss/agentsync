"""Optional Rust (PyO3 / fast-anything) hot-path accelerator with 100% pure-Python fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

# Optional compiled PyO3 extension (built when Rust/Cargo + fast-anything are available)
try:
    import _agentsync_rs as _native  # type: ignore[import-not-found]
except Exception:
    _native = None

_WS_RE = re.compile(r"\s+")
# Single-pass C-engine regex matching JSON strings, // comments, and /* */ comments
_JSONC_COMMENT_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/',
)
_JSONC_TRAILING_COMMA_RE = re.compile(
    r'"(?:\\.|[^"\\])*"|(,)([ \t\r\n]*[\]}])',
)


def is_rust_available() -> bool:
    """Return True if the optional compiled PyO3 extension is importable."""
    return _native is not None


def is_rust_enabled(preference: str = "auto") -> bool:
    """Return True when the Rust accelerator should be used (respecting FAST_ANYTHING_DISABLE=1)."""
    if preference == "python":
        return False
    if os.environ.get("FAST_ANYTHING_DISABLE", "").strip().lower() in ("1", "true", "yes"):
        return False
    return _native is not None


def runtime_engine_label(preference: str = "auto") -> str:
    """Human-readable runtime engine badge for status, doctor, and TUI Settings."""
    return "Rust Accelerated (PyO3)" if is_rust_enabled(preference) else "Pure Python"


# --- Hot Path 1: Canonical Skill Directory SHA-256 Hashing ---


def hash_skill_files(files: dict[str, bytes], *, preference: str = "auto") -> str:
    """Deterministic SHA-256 over normalized (relative_path, content) pairs."""
    if is_rust_enabled(preference) and hasattr(_native, "hash_skill_files"):
        return str(_native.hash_skill_files(files))

    h = hashlib.sha256()
    update = h.update
    # Single-pass streaming hash without intermediate dict/hex copies
    for rel, content in sorted(files.items(), key=lambda kv: kv[0].replace("\\", "/")):
        norm = rel.replace("\\", "/")
        update(norm.encode("utf-8"))
        update(b"\x00")
        update(str(len(content)).encode("ascii"))
        update(b"\x00")
        update(content)
        update(b"\x00")
    return h.hexdigest()


# --- Hot Path 2: Normalized Text & Chat Fingerprinting ---


def normalize_text_fast(text: str, *, preference: str = "auto") -> str:
    """Normalize line endings, BOM, and whitespace runs for deterministic content hashing."""
    if is_rust_enabled(preference) and hasattr(_native, "normalize_text_fast"):
        return str(_native.normalize_text_fast(text))
    cleaned = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return _WS_RE.sub(" ", cleaned).strip()


def fingerprint_chat_messages(
    project_key: str | None,
    role_text_pairs: list[tuple[str, str]],
    *,
    title: str = "",
    preference: str = "auto",
) -> str:
    """Compute stable cross-tool chat deduplication fingerprint over messages."""
    if is_rust_enabled(preference) and hasattr(_native, "fingerprint_chat_messages"):
        return str(_native.fingerprint_chat_messages(project_key or "", role_text_pairs, title))

    h = hashlib.sha256()
    update = h.update
    update((project_key or "").encode("utf-8"))
    update(b"\x1f")
    if role_text_pairs:
        for role, txt in role_text_pairs[:4]:
            update(role.encode("utf-8"))
            update(b":")
            update(normalize_text_fast(txt, preference="python")[:512].encode("utf-8"))
            update(b"\x1e")
    else:
        update(normalize_text_fast(title, preference="python")[:256].encode("utf-8"))
    return h.hexdigest()


# --- Hot Path 3: Fast Codex Rollout JSONL Scanner ---


def scan_rollout_jsonl_fast(raw_bytes: bytes, *, preference: str = "auto") -> dict[str, Any]:
    """Fast single-pass structural scan of a Codex rollout-*.jsonl file.

    Extracts BOM/CRLF flags, line count, session_meta payload, hydration lifecycle flags,
    and user/assistant messages without redundant decode/re-encode passes.
    """
    if is_rust_enabled(preference) and hasattr(_native, "scan_rollout_jsonl_fast"):
        return dict(_native.scan_rollout_jsonl_fast(raw_bytes))

    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    body = raw_bytes[3:] if has_bom else raw_bytes
    if not body.strip():
        return {
            "has_bom": has_bom,
            "line_count": 0,
            "line1": None,
            "parse_error": None,
            "has_response_msg": False,
            "has_task_started": False,
            "has_turn_context": False,
            "has_item_completed": False,
            "has_task_complete": False,
            "messages": [],
        }

    loads = json.loads
    line1: Any = None
    parse_error: tuple[int, str] | None = None
    has_response_msg = False
    has_task_started = False
    has_turn_context = False
    has_item_completed = False
    has_task_complete = False
    messages: list[tuple[str, str, str]] = []
    line_idx = 0

    for raw_line in body.splitlines():
        if not raw_line.strip():
            continue
        line_idx += 1
        try:
            obj = loads(raw_line)
        except Exception as exc:
            parse_error = (line_idx, str(exc))
            break
        if line_idx == 1:
            line1 = obj
            continue
        if not isinstance(obj, dict) or not obj.get("type") or not obj.get("timestamp"):
            parse_error = (line_idx, "missing required envelope keys ('timestamp', 'type')")
            break
        etype = obj["type"]
        pl = obj.get("payload")
        if etype == "response_item" and isinstance(pl, dict) and pl.get("type") == "message":
            has_response_msg = True
            role = pl.get("role")
            if role in ("user", "assistant"):
                parts: list[str] = []
                for c in pl.get("content") or ():
                    if isinstance(c, dict):
                        t = c.get("text")
                        if t:
                            parts.append(str(t))
                    elif isinstance(c, str):
                        parts.append(c)
                txt = "\n".join(parts).strip()
                if txt:
                    messages.append((role, txt, str(obj.get("timestamp") or "")))
        elif etype == "turn_context":
            has_turn_context = True
        elif etype == "event_msg" and isinstance(pl, dict):
            pt = pl.get("type")
            if pt == "task_started":
                has_task_started = True
            elif pt == "item_completed":
                has_item_completed = True
            elif pt == "task_complete":
                has_task_complete = True

    return {
        "has_bom": has_bom,
        "line_count": line_idx,
        "line1": line1,
        "parse_error": parse_error,
        "has_response_msg": has_response_msg,
        "has_task_started": has_task_started,
        "has_turn_context": has_turn_context,
        "has_item_completed": has_item_completed,
        "has_task_complete": has_task_complete,
        "messages": messages,
    }


# --- Hot Path 4: Fast Single-Pass JSONC Comment & Trailing-Comma Stripper ---


def strip_jsonc_fast(text: str, *, preference: str = "auto") -> str:
    """Strip // and /* */ comments and trailing commas from JSONC using C-accelerated regex passes."""
    if is_rust_enabled(preference) and hasattr(_native, "strip_jsonc_fast"):
        return str(_native.strip_jsonc_fast(text))

    def _repl_comment(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok[0] == '"':
            return tok
        if tok.startswith("/*"):
            return "\n" * tok.count("\n")
        return ""

    no_comments = _JSONC_COMMENT_RE.sub(_repl_comment, text)

    def _repl_comma(m: re.Match[str]) -> str:
        if m.group(1) is not None:
            return m.group(2)
        return m.group(0)

    return _JSONC_TRAILING_COMMA_RE.sub(_repl_comma, no_comments)
