"""Deterministic Markdown block merging, multi-tool project unification, and conflict snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentsync.models.canonical import (
    InstructionSection,
    ProjectItem,
    normalize_text_for_hash,
    parse_markdown_sections,
    render_markdown_sections,
)
from agentsync.state.state_db import ProjectStateRecord
from agentsync.utils.atomic_io import atomic_write_json, atomic_write_text

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(raw: str) -> str:
    return _SAFE_FILENAME_RE.sub("_", raw).strip("_")[:64] or "entity"


def save_conflict_snapshot(
    conflicts_dir: Path,
    category: str,
    entity_id: str,
    loser_tool: str,
    payload: Any,
) -> Path:
    """Archive an overwritten entity version inside `conflicts/` before LWW replacement."""
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    folder = conflicts_dir / f"{ts}_{category}_{_safe_filename(entity_id)}"
    folder.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        target = folder / f"{loser_tool}.md"
        atomic_write_text(target, payload)
        return target
    target = folder / f"{loser_tool}.json"
    atomic_write_json(target, payload)
    return target


def _merge_section_lines(body_a: str, body_b: str) -> str:
    paras_a = [p.strip() for p in re.split(r"\n{2,}", body_a.strip()) if p.strip()]
    seen = {normalize_text_for_hash(p) for p in paras_a}
    merged = list(paras_a)
    for p in re.split(r"\n{2,}", body_b.strip()):
        ps = p.strip()
        if ps and normalize_text_for_hash(ps) not in seen:
            merged.append(ps)
            seen.add(normalize_text_for_hash(ps))
    return "\n\n".join(merged)


def merge_markdown_texts(
    primary_md: str,
    secondary_md: str,
    primary_wins: bool = True,
) -> tuple[str, list[str]]:
    """Merge two Markdown instruction documents without duplicating repeated sections."""
    primary_sections = parse_markdown_sections(primary_md)
    secondary_sections = parse_markdown_sections(secondary_md)

    result: list[InstructionSection] = []
    seen_hashes: set[str] = set()
    heading_index: dict[tuple[int, str], int] = {}
    conflicts: list[str] = []

    for sec in primary_sections:
        if sec.section_hash in seen_hashes:
            continue
        key = (sec.heading_level, sec.normalized_heading)
        if sec.normalized_heading and key in heading_index:
            idx = heading_index[key]
            existing = result[idx]
            eb = normalize_text_for_hash(existing.body)
            nb = normalize_text_for_hash(sec.body)
            if nb and nb not in eb:
                if eb in nb:
                    result[idx] = sec
                else:
                    result[idx] = InstructionSection(
                        heading_level=existing.heading_level,
                        heading_title=existing.heading_title,
                        body=_merge_section_lines(existing.body, sec.body),
                    ).compute_hash()
            continue
        idx = len(result)
        result.append(sec)
        seen_hashes.add(sec.section_hash)
        if sec.normalized_heading:
            heading_index[key] = idx

    for sec in secondary_sections:
        if sec.section_hash in seen_hashes:
            continue
        key = (sec.heading_level, sec.normalized_heading)
        if sec.normalized_heading and key in heading_index:
            idx = heading_index[key]
            existing = result[idx]
            eb = normalize_text_for_hash(existing.body)
            nb = normalize_text_for_hash(sec.body)
            if nb == eb or (nb and nb in eb):
                continue
            if eb and eb in nb:
                result[idx] = sec
                seen_hashes.add(sec.section_hash)
                continue
            conflicts.append(sec.heading_title)
            if not primary_wins:
                result[idx] = sec
                seen_hashes.add(sec.section_hash)
            continue
        if not sec.normalized_heading:
            nb = normalize_text_for_hash(sec.body)
            if any(
                nb == normalize_text_for_hash(r.body) or nb in normalize_text_for_hash(r.body)
                for r in result
            ):
                continue
        idx = len(result)
        result.append(sec)
        seen_hashes.add(sec.section_hash)
        if sec.normalized_heading:
            heading_index[key] = idx

    return render_markdown_sections(result), conflicts


def project_content_hash(item: ProjectItem) -> str:
    payload = {
        "key": item.canonical_key,
        "display_path": item.display_path,
        "name": item.name,
        "is_git": item.is_git,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def unify_projects_multi(
    projects_by_tool: dict[str, dict[str, ProjectItem]],
    state_projects: dict[str, dict[str, ProjectStateRecord]],
    named_tools: set[str] | None = None,
) -> dict[str, ProjectItem]:
    """Unify workspaces from any number of tools keyed by canonical normalized path."""
    all_keys = sorted({k for pmap in projects_by_tool.values() for k in pmap})
    unified: dict[str, ProjectItem] = {}

    for key in all_keys:
        present: list[tuple[str, ProjectItem]] = [
            (tname, pmap[key]) for tname, pmap in projects_by_tool.items() if key in pmap
        ]
        # Prefer longest real-cased display path
        display_path = max((p.display_path for _, p in present), key=len)
        folder_base = display_path.rstrip("/").split("/")[-1].lower()
        st_map = state_projects.get(key) or {}

        # Pick canonical name: detect explicit rename only from tools that persist custom project names
        name_capable = [
            (tname, p) for tname, p in present if (named_tools is None or tname in named_tools)
        ] or present
        chosen_name: str | None = None
        for tname, p in name_capable:
            st = st_map.get(tname)
            if st and st.name and p.name != st.name:
                chosen_name = p.name
                break
        if not chosen_name:
            non_generic = [p for _, p in name_capable if p.name.lower() != folder_base]
            if non_generic:
                chosen_name = max(non_generic, key=lambda p: p.updated_at_ms).name
            else:
                chosen_name = max((p for _, p in name_capable), key=lambda p: p.updated_at_ms).name

        native_ids: dict[str, list[str]] = {}
        secondary_ids: dict[str, list[str]] = {}
        for tname, p in present:
            if tname in p.native_ids_by_tool:
                native_ids[tname] = list(p.native_ids_by_tool[tname])
            if tname in p.secondary_ids_by_tool:
                secondary_ids[tname] = list(p.secondary_ids_by_tool[tname])
        # Carry remembered IDs from sync_state.db for tools that don't currently list the project
        for tname, st in st_map.items():
            if tname not in native_ids and st.native_id:
                native_ids[tname] = [st.native_id]
            if tname not in secondary_ids and st.secondary_id:
                secondary_ids[tname] = [st.secondary_id]

        created_vals = [p.created_at_ms for _, p in present if p.created_at_ms > 0]
        unified[key] = ProjectItem(
            canonical_key=key,
            display_path=display_path,
            name=chosen_name,
            native_ids_by_tool=native_ids,
            secondary_ids_by_tool=secondary_ids,
            is_git=any(p.is_git for _, p in present),
            default_branch=next((p.default_branch for _, p in present if p.default_branch), None),
            created_at_ms=min(created_vals) if created_vals else 0,
            updated_at_ms=max((p.updated_at_ms for _, p in present), default=0),
        )
    return unified
