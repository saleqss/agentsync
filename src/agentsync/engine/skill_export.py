"""Built-in Agent Skill exporter (`agentsync install-skill`)."""

from __future__ import annotations

from agentsync.adapters.base import ToolAdapter
from agentsync.models.canonical import SkillItem

AGENTSYNC_SKILL_MD = """---
name: agentsync
description: Run safe AgentSync diagnostics, dry-run diffs, session handoffs, and visual inventory boards across AI coding agents.
---

# AgentSync Operator Skill

Use this skill when the user asks to inspect, synchronize, repair, or hand off sessions across AI coding agents (Codex, Antigravity, Claude Code, OpenCode, DeepSeek Harness, Zed, Cursor, Gemini CLI, VS Code Copilot).

## Safe Read-Only Commands (Run First)

- `agentsync discover` - List detected desktop, IDE, and terminal AI tools.
- `agentsync doctor` - Validate JSON/JSONC/TOML configs, SQLite/protobuf stores, Codex rollout headers, and broken skill symlinks.
- `agentsync patch --manual --dry-run` - Preview exact changes without writing to disk.
- `agentsync list-skills --json` - Output machine-readable skill inventory.
- `agentsync board --open` - Generate and open the offline HTML Visual Inventory Board.

## Mutating Commands (Require User Confirmation & Closed Target Apps)

- `agentsync repair` - Quarantine and regenerate broken rollouts / out-of-sync summary indexes.
- `agentsync patch --auto` - Back up, repair, and sync enabled tools automatically.
- `agentsync handoff --from <source> --to <target> [--session <id>]` - Hand off an active chat session with rules and skills verified on disk.
"""


def install_agentsync_skill(
    adapters: list[ToolAdapter], *, dry_run: bool = False
) -> list[str]:
    """Install the built-in `agentsync` SKILL.md into all skill-capable adapters."""
    skill = SkillItem(
        name="agentsync",
        frontmatter_name="agentsync",
        description="Run safe AgentSync diagnostics, dry-run diffs, and session handoffs.",
        files={"SKILL.md": AGENTSYNC_SKILL_MD.encode("utf-8")},
        source_tool="agentsync",
    ).compute_hashes()

    installed_tools: list[str] = []
    for adapter in adapters:
        if not adapter.capabilities.skills:
            continue
        if not dry_run:
            adapter.write_skill(skill)
        installed_tools.append(adapter.tool_name)
    return installed_tools
