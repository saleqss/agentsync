"""Glob-based `--include` / `--exclude` entity selectors and risk-level classification."""

from __future__ import annotations

import fnmatch
from typing import Literal

from pydantic import BaseModel, Field

RiskLevel = Literal["low", "medium", "high"]

_HIGH_RISK_PATTERNS = (
    "permission",
    "hook",
    "sandbox",
    "approval",
    "danger",
    "sudo",
    "command",
    "shell",
)


class SelectorRule(BaseModel):
    area: str
    pattern: str = "*"

    def matches(self, category: str, entity_id: str) -> bool:
        if self.area != "*" and self.area != category:
            return False
        if self.pattern == "*":
            return True
        return fnmatch.fnmatch(entity_id.lower(), self.pattern.lower())


class SyncSelector(BaseModel):
    """Evaluates `--include` and `--exclude` specs like `skills:code-*,mcp` / `mcp:internal-*`."""

    includes: list[SelectorRule] = Field(default_factory=list)
    excludes: list[SelectorRule] = Field(default_factory=list)

    @classmethod
    def parse(
        cls,
        include_specs: list[str] | None = None,
        exclude_specs: list[str] | None = None,
    ) -> SyncSelector:
        return cls(
            includes=_parse_spec_list(include_specs or []),
            excludes=_parse_spec_list(exclude_specs or []),
        )

    @property
    def is_empty(self) -> bool:
        return not self.includes and not self.excludes

    def allows_category(self, category: str) -> bool:
        if self.includes and not any(r.area in ("*", category) for r in self.includes):
            return False
        if any(r.area == category and r.pattern == "*" for r in self.excludes):
            return False
        return True

    def allows(self, category: str, entity_id: str) -> bool:
        if self.includes and not any(r.matches(category, entity_id) for r in self.includes):
            return False
        if any(r.matches(category, entity_id) for r in self.excludes):
            return False
        return True


def _parse_spec_list(specs: list[str]) -> list[SelectorRule]:
    rules: list[SelectorRule] = []
    for raw in specs:
        for token in raw.split(","):
            tok = token.strip()
            if not tok:
                continue
            if ":" in tok:
                area, pat = tok.split(":", 1)
                rules.append(SelectorRule(area=area.strip().lower(), pattern=pat.strip() or "*"))
            else:
                rules.append(SelectorRule(area=tok.lower(), pattern="*"))
    return rules


def classify_action_risk(
    category: str,
    action: str,
    entity_id: str = "",
    summary: str = "",
) -> RiskLevel:
    """Assign risk level (`low`, `medium`, `high`) for auditability in diff previews and Apply Ledger."""
    if action in ("LINK", "SKIP"):
        return "low"
    blob = f"{category} {entity_id} {summary}".lower()
    if action == "DELETE" or category in ("permissions", "hooks"):
        return "high"
    if any(tok in blob for tok in _HIGH_RISK_PATTERNS):
        return "high"
    if action == "CONFLICT" or category in ("mcp", "plugins"):
        return "medium"
    return "low"
