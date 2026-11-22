"""Portable Profile Bundle export/import (.zip) with SHA-256 manifest and secret redaction."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agentsync import __version__
from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig
from agentsync.models.canonical import InstructionDoc, McpServer, SkillItem
from agentsync.state.backup import BackupManager
from agentsync.utils.atomic_io import sha256_bytes
from agentsync.utils.env_vars import strip_literal_secrets
from agentsync.utils.process_guard import assert_apps_closed


class BundleManifest(BaseModel):
    schema_version: int = 1
    agentsync_version: str = __version__
    created_at_utc: str
    secrets_redacted: bool = True
    source_tools: list[str] = Field(default_factory=list)
    skill_count: int = 0
    mcp_count: int = 0
    has_global_instructions: bool = False
    sha256_by_entry: dict[str, str] = Field(default_factory=dict)


class BundleImportResult(BaseModel):
    dry_run: bool = False
    verified_entries: int = 0
    skills_imported: int = 0
    mcp_imported: int = 0
    instructions_imported: bool = False
    target_tools: list[str] = Field(default_factory=list)
    backup_id: str | None = None
    warnings: list[str] = Field(default_factory=list)


def _redact_mcp_server(srv: McpServer) -> McpServer:
    """Replace literal API keys/tokens inside env & headers with ${ENV_VAR} placeholders."""
    clean_env = strip_literal_secrets(srv.env)
    clean_headers = strip_literal_secrets(srv.headers)
    return srv.model_copy(update={"env": clean_env, "headers": clean_headers}).compute_hash()


def export_profile_bundle(
    output_zip: Path,
    adapters: list[ToolAdapter],
    *,
    include_secrets: bool = False,
) -> BundleManifest:
    """Export canonical skills, MCP servers, and global instructions into a SHA-256 verified zip bundle."""
    skills: dict[str, SkillItem] = {}
    mcps: dict[str, McpServer] = {}
    global_doc: InstructionDoc | None = None

    for adapter in adapters:
        if adapter.capabilities.skills:
            for name, sk in adapter.read_skills().items():
                if name not in skills or sk.updated_at_ms > skills[name].updated_at_ms:
                    skills[name] = sk
        if adapter.capabilities.mcp:
            for name, ms in adapter.read_mcp_servers().items():
                if not ms.portable:
                    continue
                if name not in mcps or ms.updated_at_ms > mcps[name].updated_at_ms:
                    mcps[name] = ms
        if adapter.capabilities.instructions:
            doc = adapter.read_global_instructions()
            if doc and (global_doc is None or doc.updated_at_ms > global_doc.updated_at_ms):
                global_doc = doc

    payloads: dict[str, bytes] = {}

    for sname, sk in sorted(skills.items()):
        for rel_path, content in sorted(sk.files.items()):
            norm_rel = rel_path.replace("\\", "/").lstrip("/")
            payloads[f"skills/{sname}/{norm_rel}"] = content

    mcp_serialized: dict[str, Any] = {}
    for mname, ms in sorted(mcps.items()):
        out_srv = ms if include_secrets else _redact_mcp_server(ms)
        mcp_serialized[mname] = out_srv.model_dump()
    payloads["mcp_servers.json"] = (
        json.dumps(mcp_serialized, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    )

    if global_doc is not None:
        payloads["instructions/global.md"] = global_doc.raw_text.encode("utf-8")

    sha_map = {k: sha256_bytes(v) for k, v in sorted(payloads.items())}
    manifest = BundleManifest(
        created_at_utc=datetime.now(UTC).isoformat(),
        secrets_redacted=not include_secrets,
        source_tools=[a.tool_name for a in adapters],
        skill_count=len(skills),
        mcp_count=len(mcps),
        has_global_instructions=global_doc is not None,
        sha256_by_entry=sha_map,
    )

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp_zip = output_zip.with_suffix(output_zip.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "manifest.json",
                json.dumps(manifest.model_dump(), indent=2, ensure_ascii=False) + "\n",
            )
            for rel, data in sorted(payloads.items()):
                zf.writestr(rel, data)
        tmp_zip.replace(output_zip)
    except BaseException:
        tmp_zip.unlink(missing_ok=True)
        raise

    return manifest


def import_profile_bundle(
    input_zip: Path,
    config: AgentSyncConfig,
    adapters: list[ToolAdapter],
    *,
    dry_run: bool = False,
    force: bool = False,
    check_processes: bool = True,
) -> BundleImportResult:
    """Verify SHA-256 manifest of a Portable Profile Bundle and apply it to target adapters."""
    if not input_zip.exists():
        raise FileNotFoundError(f"Bundle not found: {input_zip}")

    with zipfile.ZipFile(input_zip, "r") as zf:
        if "manifest.json" not in zf.namelist():
            raise ValueError("Invalid agentsync bundle: missing manifest.json")
        manifest = BundleManifest.model_validate_json(zf.read("manifest.json").decode("utf-8"))

        # Phase 1: Verify every entry's SHA-256 strictly before any disk mutation
        verified_data: dict[str, bytes] = {}
        for entry_name, expected_sha in manifest.sha256_by_entry.items():
            if entry_name not in zf.namelist():
                raise ValueError(f"Corrupted bundle: missing entry '{entry_name}'")
            raw = zf.read(entry_name)
            actual = sha256_bytes(raw)
            if actual != expected_sha:
                raise ValueError(
                    f"Corrupted bundle entry '{entry_name}': expected SHA-256 {expected_sha}, got {actual}"
                )
            verified_data[entry_name] = raw

    # Parse verified payloads
    skills_files: dict[str, dict[str, bytes]] = {}
    for entry_name, raw in verified_data.items():
        if entry_name.startswith("skills/"):
            parts = entry_name.split("/", 2)
            if len(parts) == 3:
                _, sname, rel = parts
                skills_files.setdefault(sname, {})[rel] = raw

    skill_items: list[SkillItem] = []
    for sname, fdict in sorted(skills_files.items()):
        skill_items.append(
            SkillItem(
                name=sname,
                files=fdict,
                source_tool="bundle",
                updated_at_ms=int(datetime.now(UTC).timestamp() * 1000),
            ).compute_hashes()
        )

    mcps: dict[str, McpServer] = {}
    if "mcp_servers.json" in verified_data:
        raw_mcps = json.loads(verified_data["mcp_servers.json"].decode("utf-8"))
        for mname, mdata in raw_mcps.items():
            mcps[mname] = McpServer.model_validate(mdata).compute_hash()

    global_doc: InstructionDoc | None = None
    if "instructions/global.md" in verified_data:
        global_doc = InstructionDoc.from_markdown(
            verified_data["instructions/global.md"].decode("utf-8"),
            scope="global",
            source_tool="bundle",
            updated_at_ms=int(datetime.now(UTC).timestamp() * 1000),
        )

    result = BundleImportResult(
        dry_run=dry_run,
        verified_entries=len(verified_data),
        skills_imported=len(skill_items),
        mcp_imported=len(mcps),
        instructions_imported=global_doc is not None,
        target_tools=[a.tool_name for a in adapters],
    )

    if dry_run or not adapters:
        return result

    if check_processes:
        assert_apps_closed(force=force, check_apps=[a.tool_name for a in adapters])

    # Always snapshot before importing
    b_manifest = BackupManager(config).create_backup(adapters, reason="bundle-import")
    result.backup_id = b_manifest.backup_id

    for adapter in adapters:
        if adapter.capabilities.skills:
            for sk in skill_items:
                adapter.write_skill(sk)
        if adapter.capabilities.mcp and mcps:
            existing = adapter.read_mcp_servers()
            merged = dict(existing)
            for mname, ms in mcps.items():
                if adapter.tool_name == "antigravity" and ms.transport == "sse":
                    result.warnings.append(f"Skipped legacy SSE MCP '{mname}' for antigravity")
                    continue
                merged[mname] = ms
            _, warns = adapter.write_mcp_servers(merged)
            result.warnings.extend(warns)
        if adapter.capabilities.instructions and global_doc is not None:
            if adapter.tool_name not in {"deepseek-harness", "cline"}:
                _, warns = adapter.write_global_instructions(global_doc)
                result.warnings.extend(warns)

    return result
