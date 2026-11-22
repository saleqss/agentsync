"""Reproducible generator for docs/assets/ TUI screenshots and brand showcase SVGs."""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

from agentsync import __version__
from agentsync.adapters.antigravity import AntigravityAdapter
from agentsync.adapters.claude_code import ClaudeCodeAdapter
from agentsync.adapters.codex import CodexAdapter
from agentsync.adapters.opencode import OpenCodeAdapter
from agentsync.config import AgentSyncConfig, ConfigManager, ToolConfig
from agentsync.discovery import DiscoveryEnv
from agentsync.models.canonical import (
    ChatMessage,
    ChatSession,
    InstructionDoc,
    McpServer,
    ProjectItem,
    SkillItem,
)
from agentsync.tui.app import AgentSyncApp

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "docs" / "assets"


def _seed_demo_sandbox(root: Path) -> tuple[Path, Path, DiscoveryEnv]:
    """Populate a clean, realistic, 100% synthetic sandbox with zero real personal data."""
    home = root / "home"
    appdata = root / "appdata"
    local_app = root / "localappdata"
    cfg_path = root / "config.toml"
    data_dir = root / "data"
    proj_dir = home / "workspace" / "acme-platform"
    proj_dir.mkdir(parents=True, exist_ok=True)

    codex_root = home / ".codex"
    agy_root = home / ".gemini"
    agy_app = appdata / "Antigravity"
    claude_root = home / ".claude"
    opencode_root = home / ".config" / "opencode"
    zed_root = appdata / "Zed"

    for d in (codex_root, agy_root, agy_app, claude_root, opencode_root, zed_root):
        d.mkdir(parents=True, exist_ok=True)

    # Seed Codex
    codex = CodexAdapter(ToolConfig(enabled=True, path=str(codex_root)))
    codex.write_skill(
        SkillItem(
            name="systematic-debugging",
            description="Root-cause first debugging workflow",
            files={"SKILL.md": b"---\nname: systematic-debugging\ndescription: Root-cause debugging\n---\n# Systematic Debugging\n"},
            source_tool="codex",
            updated_at_ms=1_795_370_000_000,
        ).compute_hashes()
    )
    codex.write_skill(
        SkillItem(
            name="tdd-workflow",
            description="Red-green-refactor testing discipline",
            files={"SKILL.md": b"---\nname: tdd-workflow\ndescription: TDD workflow\n---\n# TDD Workflow\n"},
            source_tool="codex",
            updated_at_ms=1_795_370_000_000,
        ).compute_hashes()
    )
    codex.write_mcp_servers(
        {
            "github": McpServer(
                name="github",
                transport="stdio",
                command="npx",
                args=["-y", "@modelcontextprotocol/server-github"],
                env={"GITHUB_TOKEN": "${GITHUB_TOKEN}"},
                source_tool="codex",
                updated_at_ms=1_795_370_000_000,
            ).compute_hash(),
            "postgres-ro": McpServer(
                name="postgres-ro",
                transport="http",
                url="https://mcp.internal.example/postgres",
                headers={"Authorization": "Bearer ${PG_MCP_TOKEN}"},
                source_tool="codex",
                updated_at_ms=1_795_370_000_000,
            ).compute_hash(),
        }
    )
    codex.write_global_instructions(
        InstructionDoc.from_markdown(
            "# Global Engineering Rules\n\n## Code Quality\n- Always run tests before committing.\n- Never commit secrets.\n",
            scope="global",
            source_tool="codex",
            updated_at_ms=1_795_370_000_000,
        )
    )
    projs = {
        "acme-platform": ProjectItem(
            canonical_key="acme-platform",
            display_path=str(proj_dir),
            name="acme-platform",
            created_at_ms=1_795_360_000_000,
            updated_at_ms=1_795_370_000_000,
        )
    }
    _, projs = codex.write_projects(projs)
    codex.write_chat(
        ChatSession(
            tool="codex",
            native_id="019a0000-0000-7000-8000-000000000101",
            title="Refactor Auth Middleware & OAuth PKCE",
            project_key="acme-platform",
            project_path=str(proj_dir),
            created_at_ms=1_795_365_000_000,
            updated_at_ms=1_795_370_000_000,
            messages=[
                ChatMessage(role="user", text="Migrate session auth to OAuth2 PKCE with unit tests.", timestamp_ms=1_795_365_000_000),
                ChatMessage(role="assistant", text="Implemented PKCE verifier flow and added 14 unit tests.", timestamp_ms=1_795_370_000_000),
            ],
        ).compute_hashes(),
        target_native_id="019a0000-0000-7000-8000-000000000101",
        projects_by_key=projs,
    )
    codex.write_chat(
        ChatSession(
            tool="codex",
            native_id="019a0000-0000-7000-8000-000000000102",
            title="Optimize PostgreSQL Connection Pool & Indexes",
            project_key="acme-platform",
            project_path=str(proj_dir),
            created_at_ms=1_795_366_000_000,
            updated_at_ms=1_795_371_000_000,
            messages=[
                ChatMessage(role="user", text="Profile slow queries on invoices table.", timestamp_ms=1_795_366_000_000),
                ChatMessage(role="assistant", text="Added composite partial index; p99 dropped from 410ms to 11ms.", timestamp_ms=1_795_371_000_000),
            ],
        ).compute_hashes(),
        target_native_id="019a0000-0000-7000-8000-000000000102",
        projects_by_key=projs,
    )

    # Seed Antigravity (slightly behind Codex so Dry-Run shows rich meaningful diff!)
    agy = AntigravityAdapter(
        ToolConfig(enabled=True, path=str(agy_root), extra_paths={"app_data": str(agy_app)})
    )
    agy.write_skill(
        SkillItem(
            name="frontend-design",
            description="High-craft responsive UI design guide",
            files={"SKILL.md": b"---\nname: frontend-design\ndescription: UI design guide\n---\n# Frontend Design\n"},
            source_tool="antigravity",
            updated_at_ms=1_795_372_000_000,
        ).compute_hashes()
    )
    (agy_app / "User" / "globalStorage").mkdir(parents=True, exist_ok=True)
    vscdb = agy_app / "User" / "globalStorage" / "state.vscdb"
    conn = sqlite3.connect(str(vscdb))
    conn.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    conn.commit()
    conn.close()

    # Seed Claude Code
    claude = ClaudeCodeAdapter(ToolConfig(enabled=True, path=str(claude_root)))
    claude.write_skill(
        SkillItem(
            name="code-review",
            description="Structured PR review checklist",
            files={"SKILL.md": b"---\nname: code-review\ndescription: PR review checklist\n---\n# Code Review\n"},
            source_tool="claude-code",
            updated_at_ms=1_795_371_000_000,
        ).compute_hashes()
    )

    # Seed OpenCode
    opencode = OpenCodeAdapter(ToolConfig(enabled=True, path=str(opencode_root)))
    opencode.write_global_instructions(
        InstructionDoc.from_markdown(
            "# Global Engineering Rules\n\n## Performance\n- Measure before optimizing.\n",
            scope="global",
            source_tool="opencode",
            updated_at_ms=1_795_371_000_000,
        )
    )

    # Seed Zed settings.json
    (zed_root / "settings.json").write_text(json.dumps({"context_servers": {}}, indent=2) + "\n", encoding="utf-8")

    cfg = AgentSyncConfig(
        locale="en",
        patch_mode="manual",
        onboarding_completed=True,
        auto_scan_on_startup=True,
        data_dir=str(data_dir),
        tools={
            "codex": ToolConfig(enabled=True, path=str(codex_root)),
            "antigravity": ToolConfig(enabled=True, path=str(agy_root), extra_paths={"app_data": str(agy_app)}),
            "claude-code": ToolConfig(enabled=True, path=str(claude_root)),
            "opencode": ToolConfig(enabled=True, path=str(opencode_root)),
            "zed": ToolConfig(enabled=False, path=str(zed_root)),
        },
    )
    ConfigManager(cfg_path).save(cfg)
    denv = DiscoveryEnv(home=home, appdata=appdata, localappdata=local_app, platform="win32")
    return cfg_path, data_dir, denv


async def _capture_tui_screenshots() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    demo_root = Path(tempfile.gettempdir()) / "agentsync-demo"
    if demo_root.exists():
        shutil.rmtree(demo_root, ignore_errors=True)
    demo_root.mkdir(parents=True, exist_ok=True)
    try:
        cfg_path, data_dir, denv = _seed_demo_sandbox(demo_root)

        # 1. Dashboard EN + Patch Diff + Handoff + Dashboard RU (136x38)
        app = AgentSyncApp(
            config_path=cfg_path,
            data_dir=data_dir,
            locale_override="en",
            discovery_env=denv,
            check_processes=False,
            show_onboarding=False,
            ascii_mode=False,
            check_updates_on_mount=False,
        )
        async with app.run_test(size=(136, 38)) as pilot:
            await pilot.pause(0.8)
            app.save_screenshot(str(ASSETS_DIR / "tui-dashboard-en.svg"))

            # Live switch to Russian
            await pilot.click("#btn-lang-ru")
            await pilot.pause(0.3)
            app.save_screenshot(str(ASSETS_DIR / "tui-dashboard-ru.svg"))

            # Switch back to EN & run Dry-Run Patch Diff
            await pilot.click("#btn-lang-en")
            await pilot.pause(0.2)
            await pilot.click("#btn-quick-dryrun")
            await pilot.pause(1.0)
            app.save_screenshot(str(ASSETS_DIR / "tui-patch-diff.svg"))

            # Switch to Session Handoff
            await pilot.click("#nav-3")
            await pilot.pause(0.2)
            await pilot.click("#btn-handoff-refresh")
            await pilot.pause(0.6)
            app.save_screenshot(str(ASSETS_DIR / "tui-handoff.svg"))

        # 2. Compact 80x24 viewport proof
        app_compact = AgentSyncApp(
            config_path=cfg_path,
            data_dir=data_dir,
            locale_override="en",
            discovery_env=denv,
            check_processes=False,
            show_onboarding=False,
            ascii_mode=False,
            check_updates_on_mount=False,
        )
        async with app_compact.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.8)
            app_compact.save_screenshot(str(ASSETS_DIR / "tui-compact-80x24.svg"))
    finally:
        shutil.rmtree(demo_root, ignore_errors=True)


def _write_brand_svgs() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    banner_svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="300" viewBox="0 0 1200 300" role="img" aria-label="AgentSync Hero Banner">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0d1117"/>
      <stop offset="55%" stop-color="#111827"/>
      <stop offset="100%" stop-color="#0b1324"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#58a6ff"/>
      <stop offset="50%" stop-color="#3fb950"/>
      <stop offset="100%" stop-color="#a371f7"/>
    </linearGradient>
    <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
      <path d="M 32 0 L 0 0 0 32" fill="none" stroke="#1f2937" stroke-width="0.8" opacity="0.5"/>
    </pattern>
  </defs>
  <rect width="1200" height="300" rx="16" fill="url(#bg)"/>
  <rect width="1200" height="300" rx="16" fill="url(#grid)"/>
  <rect x="1" y="1" width="1198" height="298" rx="15" fill="none" stroke="#30363d" stroke-width="2"/>

  <!-- Left Title Block -->
  <rect x="64" y="54" width="106" height="28" rx="14" fill="#1f6feb" opacity="0.22" stroke="#58a6ff" stroke-width="1"/>
  <text x="117" y="73" text-anchor="middle" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" font-size="13" font-weight="700" fill="#79c0ff">v{__version__}</text>

  <text x="64" y="142" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif" font-size="56" font-weight="800" letter-spacing="2" fill="#f0f6fc">AGENT<tspan fill="#58a6ff">SYNC</tspan></text>
  <rect x="64" y="156" width="360" height="4" rx="2" fill="url(#accent)"/>
  <text x="64" y="194" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif" font-size="20" fill="#c9d1d9">Universal Sync, Deep Repair &amp; Live Session Handoff for AI Coding Agents</text>
  <text x="64" y="228" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" font-size="14" fill="#8b949e">11 AI Clients  |  WAL-Safe SQLite  |  Sync-on-Close Daemon  |  Responsive TUI (EN / RU / ZH)</text>

  <!-- Right Sync Topology Graphic -->
  <g transform="translate(820, 42)">
    <rect x="0" y="0" width="320" height="216" rx="12" fill="#161b22" stroke="#30363d" stroke-width="1.5"/>
    <!-- Hub -->
    <circle cx="160" cy="108" r="36" fill="#0d1117" stroke="#58a6ff" stroke-width="2.5"/>
    <text x="160" y="104" text-anchor="middle" font-family="ui-monospace, monospace" font-size="11" font-weight="700" fill="#58a6ff">CANONICAL</text>
    <text x="160" y="119" text-anchor="middle" font-family="ui-monospace, monospace" font-size="10" fill="#3fb950">SHA-256</text>
    <!-- Connectors -->
    <line x1="76" y1="50" x2="132" y2="86" stroke="#58a6ff" stroke-width="1.8" stroke-dasharray="4 3"/>
    <line x1="244" y1="50" x2="188" y2="86" stroke="#3fb950" stroke-width="1.8" stroke-dasharray="4 3"/>
    <line x1="76" y1="166" x2="132" y2="130" stroke="#a371f7" stroke-width="1.8" stroke-dasharray="4 3"/>
    <line x1="244" y1="166" x2="188" y2="130" stroke="#f0883e" stroke-width="1.8" stroke-dasharray="4 3"/>
    <!-- Nodes -->
    <rect x="18" y="28" width="94" height="32" rx="8" fill="#0d1117" stroke="#3fb950" stroke-width="1.5"/>
    <text x="65" y="49" text-anchor="middle" font-family="ui-monospace, monospace" font-size="12" font-weight="700" fill="#e6edf3">Codex</text>

    <rect x="208" y="28" width="94" height="32" rx="8" fill="#0d1117" stroke="#58a6ff" stroke-width="1.5"/>
    <text x="255" y="49" text-anchor="middle" font-family="ui-monospace, monospace" font-size="12" font-weight="700" fill="#e6edf3">Antigravity</text>

    <rect x="18" y="156" width="94" height="32" rx="8" fill="#0d1117" stroke="#a371f7" stroke-width="1.5"/>
    <text x="65" y="177" text-anchor="middle" font-family="ui-monospace, monospace" font-size="12" font-weight="700" fill="#e6edf3">Claude Code</text>

    <rect x="208" y="156" width="94" height="32" rx="8" fill="#0d1117" stroke="#f0883e" stroke-width="1.5"/>
    <text x="255" y="177" text-anchor="middle" font-family="ui-monospace, monospace" font-size="12" font-weight="700" fill="#e6edf3">OpenCode +7</text>
  </g>
</svg>
"""
    (ASSETS_DIR / "banner.svg").write_text(banner_svg, encoding="utf-8")

    social_svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640" viewBox="0 0 1280 640" role="img" aria-label="AgentSync Social Preview">
  <defs>
    <linearGradient id="sbg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0d1117"/>
      <stop offset="100%" stop-color="#111b2e"/>
    </linearGradient>
    <linearGradient id="sbar" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#58a6ff"/>
      <stop offset="50%" stop-color="#3fb950"/>
      <stop offset="100%" stop-color="#a371f7"/>
    </linearGradient>
  </defs>
  <rect width="1280" height="640" fill="url(#sbg)"/>
  <rect x="28" y="28" width="1224" height="584" rx="20" fill="#161b22" stroke="#30363d" stroke-width="2"/>
  <text x="84" y="140" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif" font-size="76" font-weight="800" fill="#f0f6fc">AGENT<tspan fill="#58a6ff">SYNC</tspan></text>
  <rect x="84" y="160" width="460" height="6" rx="3" fill="url(#sbar)"/>
  <text x="84" y="220" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif" font-size="30" fill="#c9d1d9">Keep skills, MCP servers, rules, projects &amp; chats in sync</text>
  <text x="84" y="260" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, sans-serif" font-size="30" fill="#8b949e">across 11 desktop, IDE, and CLI AI coding agents.</text>

  <!-- Feature pills -->
  <g font-family="ui-monospace, SFMono-Regular, Consolas, monospace" font-size="18" font-weight="700">
    <rect x="84" y="316" width="250" height="48" rx="10" fill="#0d1117" stroke="#238636" stroke-width="2"/>
    <text x="209" y="346" text-anchor="middle" fill="#3fb950">WAL-Safe SQLite</text>

    <rect x="354" y="316" width="270" height="48" rx="10" fill="#0d1117" stroke="#1f6feb" stroke-width="2"/>
    <text x="489" y="346" text-anchor="middle" fill="#79c0ff">Sync-on-Close Daemon</text>

    <rect x="644" y="316" width="255" height="48" rx="10" fill="#0d1117" stroke="#8957e5" stroke-width="2"/>
    <text x="771" y="346" text-anchor="middle" fill="#d2a8ff">Session Handoff</text>

    <rect x="919" y="316" width="275" height="48" rx="10" fill="#0d1117" stroke="#d29922" stroke-width="2"/>
    <text x="1056" y="346" text-anchor="middle" fill="#e3b341">Responsive TUI</text>
  </g>

  <!-- Terminal mock bar -->
  <rect x="84" y="410" width="1110" height="148" rx="12" fill="#0d1117" stroke="#30363d" stroke-width="1.5"/>
  <circle cx="114" cy="436" r="6" fill="#f85149"/>
  <circle cx="134" cy="436" r="6" fill="#d29922"/>
  <circle cx="154" cy="436" r="6" fill="#3fb950"/>
  <text x="184" y="441" font-family="ui-monospace, Consolas, monospace" font-size="14" fill="#8b949e">agentsync v{__version__} - github.com/saleqss/agentsync</text>
  <text x="114" y="484" font-family="ui-monospace, Consolas, monospace" font-size="20" fill="#79c0ff">$ uv tool install git+https://github.com/saleqss/agentsync.git</text>
  <text x="114" y="522" font-family="ui-monospace, Consolas, monospace" font-size="20" fill="#3fb950">$ agentsync   # Launch 8-screen TUI (EN / RU / ZH)  |  agentsync autostart enable</text>
</svg>
"""
    (ASSETS_DIR / "social-preview.svg").write_text(social_svg, encoding="utf-8")

    demo_svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="360" viewBox="0 0 900 360" role="img" aria-label="AgentSync CLI and Sync-on-Close Walkthrough">
  <style>
    .line {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 14px; opacity: 0; animation: fadeIn 0.4s forwards; }}
    .l1 {{ animation-delay: 0.2s; }} .l2 {{ animation-delay: 0.8s; }} .l3 {{ animation-delay: 1.4s; }}
    .l4 {{ animation-delay: 2.0s; }} .l5 {{ animation-delay: 2.6s; }} .l6 {{ animation-delay: 3.2s; }}
    .l7 {{ animation-delay: 3.8s; }} .l8 {{ animation-delay: 4.4s; }} .l9 {{ animation-delay: 5.0s; }}
    @keyframes fadeIn {{ to {{ opacity: 1; }} }}
  </style>
  <rect width="900" height="360" rx="12" fill="#0d1117" stroke="#30363d" stroke-width="2"/>
  <rect width="900" height="34" rx="12" fill="#161b22"/>
  <circle cx="22" cy="17" r="5" fill="#f85149"/>
  <circle cx="40" cy="17" r="5" fill="#d29922"/>
  <circle cx="58" cy="17" r="5" fill="#3fb950"/>
  <text x="450" y="22" text-anchor="middle" font-family="ui-monospace, Consolas, monospace" font-size="13" fill="#8b949e">agentsync v{__version__} - Safe Sync-on-Close &amp; Deep Repair</text>

  <text x="24" y="68" class="line l1" fill="#79c0ff">$ agentsync doctor</text>
  <text x="24" y="94" class="line l2" fill="#3fb950">[OK] codex.db / state_5.sqlite / thread_history_1.sqlite - PRAGMA integrity_check: ok</text>
  <text x="24" y="120" class="line l3" fill="#e3b341">[WARN] 2 legacy Codex rollouts missing hydration events (auto-fixable)</text>
  <text x="24" y="154" class="line l4" fill="#79c0ff">$ agentsync patch --manual --dry-run</text>
  <text x="24" y="180" class="line l5" fill="#c9d1d9">  CREATE  skills   antigravity  systematic-debugging, tdd-workflow</text>
  <text x="24" y="206" class="line l6" fill="#c9d1d9">  CREATE  mcp      antigravity  github (stdio), postgres-ro (http, ${{PG_MCP_TOKEN}})</text>
  <text x="24" y="232" class="line l7" fill="#c9d1d9">  REPAIR  chats    codex        Rebuild 2 rollouts + SQLite offsets + local_thread_catalog</text>
  <text x="24" y="266" class="line l8" fill="#79c0ff">$ agentsync autostart enable</text>
  <text x="24" y="292" class="line l9" fill="#3fb950">[OK] Registered hidden logon task 'AgentSyncDaemon' (Sync-on-Close + auto SHA-256 backups)</text>
  <text x="24" y="330" font-family="ui-monospace, Consolas, monospace" font-size="12" fill="#8b949e">Zero silent writes | Zero secrets leaked | Rollback-safe SQLite transactions</text>
</svg>
"""
    (ASSETS_DIR / "demo.svg").write_text(demo_svg, encoding="utf-8")


def main() -> None:
    _write_brand_svgs()
    asyncio.run(_capture_tui_screenshots())
    print("Generated showcase assets in", ASSETS_DIR)
    for p in sorted(ASSETS_DIR.iterdir()):
        print(f"  - {p.name} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
