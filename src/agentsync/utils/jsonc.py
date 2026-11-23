"""Pure-Python JSONC parser and comment-preserving surgical updater (for Zed, OpenCode, VS Code)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentsync.accel import strip_jsonc_fast
from agentsync.utils.atomic_io import atomic_write_text


def strip_jsonc_comments(text: str) -> str:
    """Strip `//` line comments, `/* */` block comments, and trailing commas outside JSON strings."""
    return strip_jsonc_fast(text)


def loads_jsonc(text: str) -> Any:
    """Parse JSON or JSONC text (ignoring UTF-8 BOM, comments, and trailing commas)."""
    cleaned = text.lstrip("\ufeff").strip()
    if not cleaned:
        return {}
    return json.loads(strip_jsonc_comments(cleaned))


def _find_top_level_key_span(text: str, target_key: str) -> tuple[int, int] | None:
    """Locate the (val_start, val_end) character span of a top-level object property in JSONC."""
    i = 0
    n = len(text)
    depth = 0
    in_str = False
    escaped = False

    def _skip_ws_comments(pos: int) -> int:
        while pos < n:
            if text[pos] in (" ", "\t", "\r", "\n"):
                pos += 1
            elif text[pos : pos + 2] == "//":
                pos += 2
                while pos < n and text[pos] != "\n":
                    pos += 1
            elif text[pos : pos + 2] == "/*":
                pos += 2
                while pos + 1 < n and text[pos : pos + 2] != "*/":
                    pos += 1
                pos += 2
            else:
                break
        return pos

    def _read_string(pos: int) -> tuple[str, int]:
        start = pos
        pos += 1
        esc = False
        while pos < n:
            c = text[pos]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                return json.loads(text[start : pos + 1]), pos + 1
            pos += 1
        return "", n

    def _skip_value(pos: int) -> int:
        pos = _skip_ws_comments(pos)
        if pos >= n:
            return pos
        c = text[pos]
        if c == '"':
            _, end = _read_string(pos)
            return end
        if c in ("{", "["):
            d = 0
            s_in = False
            s_esc = False
            while pos < n:
                ch = text[pos]
                if s_in:
                    if s_esc:
                        s_esc = False
                    elif ch == "\\":
                        s_esc = True
                    elif ch == '"':
                        s_in = False
                    pos += 1
                    continue
                if ch == '"':
                    s_in = True
                    pos += 1
                    continue
                if text[pos : pos + 2] == "//":
                    pos += 2
                    while pos < n and text[pos] != "\n":
                        pos += 1
                    continue
                if text[pos : pos + 2] == "/*":
                    pos += 2
                    while pos + 1 < n and text[pos : pos + 2] != "*/":
                        pos += 1
                    pos += 2
                    continue
                if ch in ("{", "["):
                    d += 1
                elif ch in ("}", "]"):
                    d -= 1
                    if d == 0:
                        return pos + 1
                pos += 1
            return n
        while pos < n and text[pos] not in (",", "}", "]", "\n", "\r", "/"):
            pos += 1
        return pos

    while i < n:
        if in_str:
            if escaped:
                escaped = False
            elif text[i] == "\\":
                escaped = True
            elif text[i] == '"':
                in_str = False
            i += 1
            continue
        if text[i : i + 2] == "//":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text[i : i + 2] == "/*":
            i += 2
            while i + 1 < n and text[i : i + 2] != "*/":
                i += 1
            i += 2
            continue
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
            if depth == 1:
                while i < n:
                    i = _skip_ws_comments(i)
                    if i >= n or text[i] == "}":
                        return None
                    if text[i] != '"':
                        i += 1
                        continue
                    key, after_key = _read_string(i)
                    colon = _skip_ws_comments(after_key)
                    if colon >= n or text[colon] != ":":
                        i = colon + 1
                        continue
                    val_start = _skip_ws_comments(colon + 1)
                    val_end = _skip_value(val_start)
                    if key == target_key:
                        return val_start, val_end
                    nxt = _skip_ws_comments(val_end)
                    if nxt < n and text[nxt] == ",":
                        i = nxt + 1
                    else:
                        i = nxt
            continue
        if ch == '"':
            in_str = True
        i += 1
    return None


def _indent_json_block(value: Any, base_indent: str = "  ") -> str:
    dumped = json.dumps(value, indent=2, ensure_ascii=False)
    lines = dumped.splitlines()
    if len(lines) <= 1:
        return dumped
    return lines[0] + "\n" + "\n".join(base_indent + ln for ln in lines[1:])


def patch_jsonc_text(original: str, updates: dict[str, Any]) -> str:
    """Surgically update top-level keys in a JSONC document while preserving all comments and untouched keys."""
    raw = original.lstrip("\ufeff")
    if not raw.strip():
        return json.dumps(updates, indent=2, ensure_ascii=False) + "\n"

    # Validate original parses cleanly first
    parsed = loads_jsonc(raw)
    if not isinstance(parsed, dict):
        merged = dict(updates)
        return json.dumps(merged, indent=2, ensure_ascii=False) + "\n"

    text = raw
    missing_keys: dict[str, Any] = {}

    for key, val in updates.items():
        span = _find_top_level_key_span(text, key)
        if span is not None:
            v_start, v_end = span
            # Detect indentation of the line containing this key
            line_start = text.rfind("\n", 0, v_start) + 1
            indent_m = re.match(r"^[ \t]*", text[line_start:v_start])
            indent = indent_m.group(0) if indent_m else "  "
            replacement = _indent_json_block(val, base_indent=indent)
            text = text[:v_start] + replacement + text[v_end:]
        else:
            missing_keys[key] = val

    if missing_keys:
        close_idx = text.rfind("}")
        if close_idx == -1:
            merged = {**parsed, **updates}
            return json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
        before_close = text[:close_idx].rstrip()
        needs_comma = len(parsed) > 0 and not before_close.endswith((",", "{"))
        insertion_lines: list[str] = []
        for idx, (k, v) in enumerate(missing_keys.items()):
            comma = "," if idx < len(missing_keys) - 1 else ""
            rendered_val = _indent_json_block(v, base_indent="  ")
            insertion_lines.append(f'  {json.dumps(k)}: {rendered_val}{comma}')
        prefix = before_close + (",\n" if needs_comma else "\n")
        suffix = "\n" + text[close_idx:].lstrip()
        text = prefix + "\n".join(insertion_lines) + suffix

    if not text.endswith("\n"):
        text += "\n"

    # Guarantee output is still valid JSONC before returning
    loads_jsonc(text)
    return text


def patch_jsonc_file(path: Path, updates: dict[str, Any]) -> bool:
    """Atomically patch top-level keys of a JSONC file without losing user comments or formatting."""
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    patched = patch_jsonc_text(original, updates)
    return atomic_write_text(path, patched, validator=lambda s: loads_jsonc(s))
