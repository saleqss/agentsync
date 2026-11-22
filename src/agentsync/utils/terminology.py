"""Lossless bidirectional semantic terminology translator and strict-vocab guard."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class VocabWarning(BaseModel):
    token: str
    origin_host: str
    target_host: str
    message: str


class TranslationReport(BaseModel):
    translated_text: str
    replacements: list[tuple[str, str]] = Field(default_factory=list)
    warnings: list[VocabWarning] = Field(default_factory=list)


@lru_cache(maxsize=2)
def load_terminology_map(override_path: str | None = None) -> dict[str, Any]:
    path = Path(override_path) if override_path else (_DATA_DIR / "terminology_map.json")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=2)
def load_strict_vocab(override_path: str | None = None) -> dict[str, list[str]]:
    path = Path(override_path) if override_path else (_DATA_DIR / "host_strict_vocab.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: list(v) for k, v in (data.get("host_tokens") or {}).items()}


def _token_regex(token: str) -> re.Pattern[str]:
    escaped = re.escape(token)
    # Match whole token boundaries while allowing sentence-ending punctuation like "NotebookEdit."
    return re.compile(rf"(?<![A-Za-z0-9_/-]){escaped}(?!(?:[A-Za-z0-9_-]|\.[A-Za-z0-9_]))")


def translate_host_tokens(
    text: str,
    source_host: str,
    target_host: str,
    *,
    strict_vocab_guard: bool = True,
) -> TranslationReport:
    """Translate host-specific tool/file/permission tokens cleanly and check for unmappable host-only tokens."""
    if not text or source_host == target_host:
        return TranslationReport(translated_text=text)

    tmap = load_terminology_map()
    pairs: list[tuple[str, str]] = []
    for layer in tmap.get("layers") or []:
        for rule in layer.get("rules") or []:
            src_tok = rule.get(source_host)
            tgt_tok = rule.get(target_host)
            if isinstance(src_tok, str) and isinstance(tgt_tok, str) and src_tok != tgt_tok:
                pairs.append((src_tok, tgt_tok))

    # Apply longest tokens first to prevent partial prefix clobbering
    pairs.sort(key=lambda kv: len(kv[0]), reverse=True)

    # Use temporary sentinel placeholders to make multi-rule translation collision-free
    result = text
    sentinels: list[tuple[str, str, str]] = []
    replacements: list[tuple[str, str]] = []

    for idx, (src_tok, tgt_tok) in enumerate(pairs):
        pat = _token_regex(src_tok)
        if pat.search(result):
            sentinel = f"\x00AGENTSYNC_TOK_{idx}\x00"
            result = pat.sub(sentinel, result)
            sentinels.append((sentinel, src_tok, tgt_tok))

    for sentinel, src_tok, tgt_tok in sentinels:
        result = result.replace(sentinel, tgt_tok)
        replacements.append((src_tok, tgt_tok))

    warnings: list[VocabWarning] = []
    if strict_vocab_guard:
        warnings = scan_strict_vocab(result, target_host=target_host)

    return TranslationReport(
        translated_text=result,
        replacements=replacements,
        warnings=warnings,
    )


def scan_strict_vocab(text: str, target_host: str) -> list[VocabWarning]:
    """Detect raw host-only tokens from other tools lingering inside `target_host`'s text."""
    if not text:
        return []
    vocab = load_strict_vocab()
    target_allowed = set(vocab.get(target_host, []))
    warnings: list[VocabWarning] = []
    seen: set[str] = set()

    for origin_host, tokens in vocab.items():
        if origin_host == target_host:
            continue
        for tok in tokens:
            if tok in target_allowed or tok in seen:
                continue
            if _token_regex(tok).search(text):
                seen.add(tok)
                warnings.append(
                    VocabWarning(
                        token=tok,
                        origin_host=origin_host,
                        target_host=target_host,
                        message=(
                            f"Host-only token '{tok}' (from {origin_host}) found in {target_host} text "
                            "with no native equivalent."
                        ),
                    )
                )
    return warnings
