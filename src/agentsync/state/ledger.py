"""Immutable per-item SHA-256 Apply Ledger, Handoff Import Ledger, and Protected Adapted Skills store."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agentsync.config import AgentSyncConfig
from agentsync.utils.atomic_io import atomic_write_json
from agentsync.utils.selectors import RiskLevel


class LedgerItem(BaseModel):
    category: str
    target_tool: str
    entity_id: str
    action: str
    risk_level: RiskLevel = "low"
    before_sha256: str = ""
    after_sha256: str = ""
    backup_path: str = ""
    summary: str = ""


class ApplyLedgerEntry(BaseModel):
    run_id: str
    timestamp_utc: str
    mode: str
    plan_hash: str
    backup_id: str | None = None
    tools: list[str] = Field(default_factory=list)
    items: list[LedgerItem] = Field(default_factory=list)


class HandoffLedgerRecord(BaseModel):
    source_tool: str
    source_session_id: str
    target_tool: str
    target_session_id: str
    content_hash: str
    project_path: str | None = None
    verified_on_disk: bool = False
    transferred_at_utc: str


class AdaptedSkillRecord(BaseModel):
    skill_name: str
    tool_name: str
    adapted_hash: str
    reason: str = "manual-host-adaptation"
    marked_at_utc: str


def compute_plan_hash(items: list[dict[str, Any]]) -> str:
    canonical = json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class LedgerStore:
    """Manages immutable Apply Ledgers, Handoff dedupe ledgers, and Protected Adapted Skills."""

    def __init__(self, config: AgentSyncConfig) -> None:
        self.config = config

    # --- Apply Ledger ---

    def write_apply_ledger(
        self,
        *,
        run_id: str,
        mode: str,
        tools: list[str],
        items: list[LedgerItem],
        backup_id: str | None = None,
    ) -> tuple[ApplyLedgerEntry, Path]:
        ts = datetime.now(UTC)
        plan_hash = compute_plan_hash([i.model_dump() for i in items])
        entry = ApplyLedgerEntry(
            run_id=run_id,
            timestamp_utc=ts.isoformat(),
            mode=mode,
            plan_hash=plan_hash,
            backup_id=backup_id,
            tools=sorted(set(tools)),
            items=items,
        )
        fname = f"{ts.strftime('%Y%m%d-%H%M%S')}_{run_id[:8]}.json"
        out_path = self.config.ledgers_dir / fname
        atomic_write_json(out_path, entry.model_dump(), ApplyLedgerEntry)
        return entry, out_path

    def list_apply_ledgers(self, limit: int = 20) -> list[ApplyLedgerEntry]:
        if not self.config.ledgers_dir.exists():
            return []
        entries: list[ApplyLedgerEntry] = []
        for f in sorted(self.config.ledgers_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                entries.append(ApplyLedgerEntry.model_validate_json(f.read_text(encoding="utf-8")))
            except Exception:
                continue
        return entries

    # --- Handoff Import Ledger (content-hash dedupe) ---

    def _load_handoffs(self) -> list[HandoffLedgerRecord]:
        p = self.config.handoffs_ledger_path
        if not p.exists():
            return []
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            return [HandoffLedgerRecord.model_validate(x) for x in raw]
        except Exception:
            return []

    def find_existing_handoff(
        self, source_tool: str, source_session_id: str, target_tool: str, content_hash: str
    ) -> HandoffLedgerRecord | None:
        for rec in reversed(self._load_handoffs()):
            if (
                rec.source_tool == source_tool
                and rec.source_session_id == source_session_id
                and rec.target_tool == target_tool
                and rec.content_hash == content_hash
            ):
                return rec
        return None

    def record_handoff(self, record: HandoffLedgerRecord) -> None:
        records = self._load_handoffs()
        records.append(record)
        atomic_write_json(self.config.handoffs_ledger_path, [r.model_dump() for r in records])

    # --- Protected Adapted Skills ---

    def list_adapted_skills(self) -> dict[tuple[str, str], AdaptedSkillRecord]:
        p = self.config.adapted_skills_path
        if not p.exists():
            return {}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            out: dict[tuple[str, str], AdaptedSkillRecord] = {}
            for item in raw:
                rec = AdaptedSkillRecord.model_validate(item)
                out[(rec.skill_name, rec.tool_name)] = rec
            return out
        except Exception:
            return {}

    def mark_adapted_skill(
        self, skill_name: str, tool_name: str, adapted_hash: str, reason: str = "manual-host-adaptation"
    ) -> AdaptedSkillRecord:
        current = self.list_adapted_skills()
        rec = AdaptedSkillRecord(
            skill_name=skill_name,
            tool_name=tool_name,
            adapted_hash=adapted_hash,
            reason=reason,
            marked_at_utc=datetime.now(UTC).isoformat(),
        )
        current[(skill_name, tool_name)] = rec
        atomic_write_json(
            self.config.adapted_skills_path,
            [v.model_dump() for v in current.values()],
        )
        return rec

    def is_protected_adapted_skill(self, skill_name: str, tool_name: str, current_hash: str) -> bool:
        rec = self.list_adapted_skills().get((skill_name, tool_name))
        return bool(rec is not None and rec.adapted_hash == current_hash)
