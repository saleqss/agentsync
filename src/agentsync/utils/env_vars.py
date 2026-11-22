"""Lossless cross-format environment variable placeholder translation without expanding secrets."""

from __future__ import annotations

import re
from typing import Any, Literal

EnvVarStyle = Literal["canonical", "vscode", "opencode"]

# Matches ${VAR}, ${env:VAR}, and {env:VAR}
_CANONICAL_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VSCODE_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_OPENCODE_RE = re.compile(r"(?<!\$)\{env:([A-Za-z_][A-Za-z0-9_]*)\}")

_TOOL_ENV_STYLE: dict[str, EnvVarStyle] = {
    "codex": "canonical",
    "antigravity": "canonical",
    "claude-code": "canonical",
    "zed": "canonical",
    "gemini-cli": "canonical",
    "deepseek-harness": "canonical",
    "opencode": "opencode",
    "cursor": "vscode",
    "windsurf": "vscode",
    "cline": "vscode",
    "vscode-copilot": "vscode",
}

_SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_\-]{25,}|xox[baprs]-[A-Za-z0-9\-]{10,})"
)
_SENSITIVE_ENV_KEY_RE = re.compile(r"(token|secret|password|passwd|api[_-]?key|auth|bearer|credential)", re.I)


def style_for_tool(tool_name: str) -> EnvVarStyle:
    return _TOOL_ENV_STYLE.get(tool_name, "canonical")


def to_canonical_env_vars(text: str) -> str:
    """Normalize `${env:VAR}` and `{env:VAR}` into canonical `${VAR}` without expanding values."""
    step1 = _VSCODE_RE.sub(r"${\1}", text)
    return _OPENCODE_RE.sub(r"${\1}", step1)


def from_canonical_env_vars(text: str, style: EnvVarStyle) -> str:
    """Render canonical `${VAR}` into the target client's native env-var reference syntax."""
    normalized = to_canonical_env_vars(text)
    if style == "canonical":
        return normalized
    if style == "vscode":
        return _CANONICAL_RE.sub(r"${env:\1}", normalized)
    if style == "opencode":
        return _CANONICAL_RE.sub(r"{env:\1}", normalized)
    return normalized


def translate_env_vars_for_tool(value: Any, tool_name: str, *, to_canonical: bool = False) -> Any:
    """Recursively translate env-var references across dicts, lists, and strings without secret expansion."""
    style = style_for_tool(tool_name)
    if isinstance(value, str):
        return to_canonical_env_vars(value) if to_canonical else from_canonical_env_vars(value, style)
    if isinstance(value, list):
        return [translate_env_vars_for_tool(v, tool_name, to_canonical=to_canonical) for v in value]
    if isinstance(value, dict):
        return {
            k: translate_env_vars_for_tool(v, tool_name, to_canonical=to_canonical)
            for k, v in value.items()
        }
    return value


def strip_literal_secrets(mapping: dict[str, str], *, prefix: str = "") -> dict[str, str]:
    """Replace literal secret values with safe `${ENV_VAR}` placeholders while preserving existing placeholders."""
    result: dict[str, str] = {}
    for k, v in mapping.items():
        s = str(v)
        # Preserve already-parameterized env references untouched
        if _CANONICAL_RE.search(s) or _VSCODE_RE.search(s) or _OPENCODE_RE.search(s):
            result[k] = s
            continue
        is_secret = bool(_SECRET_VALUE_RE.search(s)) or (
            bool(_SENSITIVE_ENV_KEY_RE.search(k)) and len(s.strip()) >= 8
        )
        if is_secret:
            env_name = re.sub(r"[^A-Za-z0-9_]+", "_", f"{prefix}_{k}" if prefix else k).upper().strip("_")
            if s.lower().startswith("bearer "):
                result[k] = f"Bearer ${{{env_name}}}"
            else:
                result[k] = f"${{{env_name}}}"
        else:
            result[k] = s
    return result
