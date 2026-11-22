"""Multi-language localization engine (English, Russian, Simplified Chinese) with instant live switching."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

SUPPORTED_LOCALES: tuple[str, ...] = ("en", "ru", "zh")
LOCALE_LABELS: dict[str, str] = {
    "en": "English",
    "ru": "Русский",
    "zh": "简体中文",
}

_LOCALES_DIR = Path(__file__).resolve().parent / "locales"
_CACHE: dict[str, dict[str, str]] = {}
_LOCK = threading.Lock()
_CURRENT_LOCALE = "en"


def load_locale_catalog(locale: str) -> dict[str, str]:
    """Load and cache the flat key->template mapping for `locale`."""
    code = locale if locale in SUPPORTED_LOCALES else "en"
    with _LOCK:
        if code not in _CACHE:
            path = _LOCALES_DIR / f"{code}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            _CACHE[code] = {str(k): str(v) for k, v in data.items()}
        return _CACHE[code]


def set_locale(locale: str) -> str:
    """Set active global UI locale and return the normalized code."""
    global _CURRENT_LOCALE
    code = locale.strip().lower()
    if code not in SUPPORTED_LOCALES:
        code = "en"
    with _LOCK:
        _CURRENT_LOCALE = code
    return code


def get_locale() -> str:
    with _LOCK:
        return _CURRENT_LOCALE


def cycle_locale() -> str:
    """Advance locale en -> ru -> zh -> en for hotkey switching."""
    cur = get_locale()
    idx = SUPPORTED_LOCALES.index(cur) if cur in SUPPORTED_LOCALES else 0
    nxt = SUPPORTED_LOCALES[(idx + 1) % len(SUPPORTED_LOCALES)]
    return set_locale(nxt)


def t(key: str, *, locale: str | None = None, **params: Any) -> str:
    """Translate `key` using the active or specified locale with safe English fallback."""
    code = locale or get_locale()
    cat = load_locale_catalog(code)
    template = cat.get(key)
    if template is None and code != "en":
        template = load_locale_catalog("en").get(key)
    if template is None:
        template = key
    if not params:
        return template
    try:
        return template.format(**params)
    except Exception:
        return template
