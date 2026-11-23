"""Typer + Rich + Textual CLI entry point for agentsync (100% backward compatible)."""

from __future__ import annotations

import json
import sys
import webbrowser
from collections import Counter
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agentsync import __version__
from agentsync.accel import runtime_engine_label
from agentsync.adapters.base import ToolAdapter
from agentsync.adapters.registry import get_registry
from agentsync.config import (
    ALL_ENTITIES,
    EDITABLE_CONFIG_KEYS,
    AgentSyncConfig,
    ConfigManager,
    EntityType,
    PatchMode,
    SyncOptions,
    SyncScope,
    ToolConfig,
    set_config_setting,
)
from agentsync.daemon import (
    read_daemon_status,
    restart_daemon,
    run_daemon_loop,
    send_daemon_command,
    start_daemon_background,
    stop_daemon,
)
from agentsync.discovery import discover_tools, merge_discovery_into_config
from agentsync.engine.autostart import (
    autostart_status,
    detect_split_installations,
    disable_autostart,
    enable_autostart,
    purge_legacy_autostart_artifacts,
)
from agentsync.engine.board import generate_inventory_board
from agentsync.engine.bootstrap import BootstrapEngine
from agentsync.engine.bundle import export_profile_bundle, import_profile_bundle
from agentsync.engine.diagnostics import DiagnosticsEngine, load_repair_queue
from agentsync.engine.doctor import DoctorEngine
from agentsync.engine.handoff import HandoffEngine, LaunchMode
from agentsync.engine.patch import PatchEngine
from agentsync.engine.skill_export import install_agentsync_skill
from agentsync.engine.support import create_support_bundle
from agentsync.engine.sync import SyncEngine
from agentsync.engine.updater import check_for_updates, perform_update
from agentsync.engine.watcher import SyncWatcher
from agentsync.engine.workspace import WorkspaceEngine
from agentsync.i18n import set_locale
from agentsync.models.canonical import SyncPlan
from agentsync.state.backup import BackupManager
from agentsync.state.ledger import LedgerStore
from agentsync.state.state_db import StateStore
from agentsync.utils.env_adapt import detect_environment, enforce_utf8_io
from agentsync.utils.process_guard import AppsRunningError, detect_running_apps
from agentsync.utils.secrets import mask_text

enforce_utf8_io()

app = typer.Typer(
    name="agentsync",
    help="Universal cross-platform synchronization, patching, context-handoff, and TUI suite for AI coding agents.",
    invoke_without_command=True,
)
tools_app = typer.Typer(help="Manage enabled AI tools, paths, and per-tool entity filters.")
workspace_app = typer.Typer(help="Unified project workspace instruction & shared context synchronization.")
autostart_app = typer.Typer(help="Configure native windowless background autostart on login.")
daemon_app = typer.Typer(help="Control the single-instance background synchronization daemon (agentsyncw).")
config_app = typer.Typer(help="Inspect and modify AgentSync settings with live daemon hot-reload.")
bundle_app = typer.Typer(help="Export or import Portable Profile Bundles (.zip) with SHA-256 verification.")
backup_app = typer.Typer(help="Create manual verified snapshots and verify backup SHA-256 + SQLite integrity.")
app.add_typer(tools_app, name="tools")
app.add_typer(workspace_app, name="workspace")
app.add_typer(autostart_app, name="autostart")
app.add_typer(daemon_app, name="daemon")
app.add_typer(config_app, name="config")
app.add_typer(bundle_app, name="bundle")
app.add_typer(backup_app, name="backup")
console = Console()

_ACTION_COLORS = {
    "CREATE": "green",
    "UPDATE": "cyan",
    "MERGE": "blue",
    "LINK": "magenta",
    "DELETE": "red",
    "SKIP": "yellow",
    "CONFLICT": "bold red",
    "REPAIR": "bold cyan",
}

_RISK_COLORS = {
    "low": "green",
    "medium": "yellow",
    "high": "bold red",
}


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"agentsync {__version__}")
        raise typer.Exit()


@app.callback()
def _root_callback(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show agentsync version and exit.",
    ),
    lang: str | None = typer.Option(
        None,
        "--lang",
        help="Override interface language (en, ru, zh).",
    ),
    no_tui: bool = typer.Option(
        False,
        "--no-tui",
        help="Disable interactive TUI launch when invoked without a subcommand.",
    ),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    _ = version
    if lang:
        set_locale(lang)
    if ctx.invoked_subcommand is None:
        env = detect_environment()
        if not no_tui and env.tui_recommended:
            from agentsync.tui import launch_tui

            launch_tui(config_path=config_file, data_dir=data_dir, locale_override=lang)
            raise typer.Exit()
        console.print(ctx.get_help())
        raise typer.Exit()


def _load_effective_config(
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> tuple[ConfigManager, AgentSyncConfig]:
    mgr = ConfigManager(config_path)
    cfg = mgr.load()
    if not cfg.tools:
        discovered = discover_tools()
        cfg = merge_discovery_into_config(cfg, discovered)
    if data_dir is not None:
        cfg.data_dir = str(data_dir)
    return mgr, cfg


def _build_adapters(cfg: AgentSyncConfig, targets: list[str] | None = None) -> list[ToolAdapter]:
    registry = get_registry()
    adapters = registry.create_enabled(cfg)
    if targets:
        allowed = {x.strip() for item in targets for x in item.split(",") if x.strip()}
        return [a for a in adapters if a.tool_name in allowed]
    return adapters


def _render_plan(plan: SyncPlan, show_all: bool = False) -> None:
    status_badge = (
        "[yellow](DRY-RUN - no files modified)[/yellow]"
        if plan.dry_run
        else "[green](APPLIED)[/green]"
    )
    title = f"[bold]{plan.mode.upper()}[/bold] {status_badge}"
    if plan.backup_id:
        title += f" | Backup: [cyan]{plan.backup_id}[/cyan]"

    summary_table = Table(title=f"Plan Summary - {title}")
    summary_table.add_column("Category", style="bold")
    for act in ("DELETE", "CREATE", "UPDATE", "MERGE", "LINK", "CONFLICT", "SKIP"):
        summary_table.add_column(act, justify="right", style=_ACTION_COLORS[act])

    counts_by_cat: dict[str, Counter[str]] = {c: Counter() for c in ALL_ENTITIES}
    for a in plan.actions:
        counts_by_cat.setdefault(a.category, Counter())[a.action] += 1

    for cat in ALL_ENTITIES:
        c = counts_by_cat[cat]
        summary_table.add_row(
            cat,
            str(c["DELETE"]),
            str(c["CREATE"]),
            str(c["UPDATE"]),
            str(c["MERGE"]),
            str(c["LINK"]),
            str(c["CONFLICT"]),
            str(c["SKIP"]),
        )
    console.print(summary_table)

    if plan.is_empty:
        skip_note = f" ({len(plan.skipped_actions)} skipped)" if plan.skipped_actions else ""
        console.print(f"[green]No changes required (0 diff){skip_note}. All tools are in sync.[/green]")
        if not plan.skipped_actions:
            return

    detail_table = Table(title="Detailed Diff Report (secrets redacted)", expand=True)
    detail_table.add_column("Category", style="bold", no_wrap=True)
    detail_table.add_column("Action", no_wrap=True)
    detail_table.add_column("Target", overflow="fold", ratio=1)
    detail_table.add_column("Risk", no_wrap=True)
    detail_table.add_column("Entity", overflow="fold", ratio=1)
    detail_table.add_column("Summary", overflow="fold", ratio=2)

    skill_bulk: dict[tuple[str, str], list[str]] = {}
    for act in plan.actions:
        if not show_all and act.category == "skills" and act.action in ("DELETE", "CREATE"):
            skill_bulk.setdefault((act.action, act.target_tool), []).append(act.entity_id)
            continue
        color = _ACTION_COLORS.get(act.action, "white")
        rcolor = _RISK_COLORS.get(act.risk_level, "white")
        detail_table.add_row(
            act.category,
            f"[{color}]{act.action}[/{color}]",
            act.target_tool,
            f"[{rcolor}]{act.risk_level}[/{rcolor}]",
            mask_text(act.entity_id),
            mask_text(act.summary),
        )

    for (act_kind, target_tool), names in sorted(skill_bulk.items()):
        color = _ACTION_COLORS.get(act_kind, "white")
        preview = ", ".join(names[:8])
        if len(names) > 8:
            preview += f" ... (+{len(names) - 8} more)"
        detail_table.add_row(
            "skills",
            f"[{color}]{act_kind}[/{color}]",
            target_tool,
            "[green]low[/green]",
            f"{len(names)} skills",
            preview,
        )

    console.print(detail_table)

    if plan.ledger_path:
        console.print(f"[dim]Apply Ledger: {plan.ledger_path}[/dim]")

    if plan.warnings:
        console.print(
            Panel(
                "\n".join(f"- {mask_text(w)}" for w in plan.warnings),
                title="[yellow]Warnings[/yellow]",
                border_style="yellow",
            )
        )


# --- Interactive TUI Launcher ---


@app.command("ui")
def cmd_ui(
    lang: str | None = typer.Option(None, "--lang", help="Interface language (en, ru, zh)."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Launch the full-featured interactive Terminal UI (TUI)."""
    from agentsync.tui import launch_tui

    launch_tui(config_path=config_file, data_dir=data_dir, locale_override=lang)


# --- Discovery & Tool Management Commands ---


@app.command("discover")
def cmd_discover(
    save: bool = typer.Option(False, "--save", help="Persist discovered tools to config.toml."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
) -> None:
    """Auto-detect installed desktop, IDE, and CLI AI coding tools, paths, and capabilities."""
    discovered = discover_tools()
    mgr = ConfigManager(config_file)
    existing_cfg = mgr.load()

    tbl = Table(title="Auto-Discovered AI Coding Tools", expand=True)
    tbl.add_column("Tool", style="bold", overflow="fold", ratio=1)
    tbl.add_column("Type", no_wrap=True)
    tbl.add_column("Inst", no_wrap=True)
    tbl.add_column("Status", no_wrap=True)
    tbl.add_column("Detected Path", overflow="fold", ratio=2)
    tbl.add_column("Entities", overflow="fold", ratio=1)

    for name, info in sorted(discovered.items(), key=lambda kv: (not kv[1].installed, kv[0])):
        tcfg = existing_cfg.tools.get(name)
        enabled_str = (
            "[green]enabled[/green]"
            if (tcfg.enabled if tcfg else info.installed)
            else "[dim]disabled[/dim]"
        )
        inst_str = "[green]yes[/green]" if info.installed else "[dim]no[/dim]"
        path_display = (tcfg.path if tcfg and tcfg.path else info.root_path) or "-"
        tbl.add_row(
            f"{info.display_name} ({name})",
            info.client_kind,
            inst_str,
            enabled_str,
            path_display,
            ", ".join(info.supported_entities),
        )
    console.print(tbl)

    if save:
        merged = merge_discovery_into_config(existing_cfg, discovered)
        mgr.save(merged)
        console.print(f"[green]Saved discovered tool configuration to {mgr.config_path}[/green]")
    else:
        console.print(
            f"[dim]Read-only scan complete. Run 'agentsync discover --save' to write {mgr.config_path}.[/dim]"
        )


@tools_app.command("list")
def cmd_tools_list(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
) -> None:
    """List registered tools, configured paths, enabled state, and capabilities."""
    _, cfg = _load_effective_config(config_file)
    registry = get_registry()
    discovered = discover_tools()

    tbl = Table(title="Registered AI Tools & Capabilities", expand=True)
    tbl.add_column("Tool", style="bold", no_wrap=True)
    tbl.add_column("Type", no_wrap=True)
    tbl.add_column("Enabled", no_wrap=True)
    tbl.add_column("Path", overflow="fold", ratio=2)
    tbl.add_column("Adapter Capabilities", overflow="fold", ratio=1)

    for name in registry.list_names():
        cls = registry.get(name)
        tcfg = cfg.tools.get(name) or ToolConfig(
            enabled=False, path=discovered.get(name).root_path if name in discovered else None
        )
        caps = [e for e in ALL_ENTITIES if cls.capabilities.supports(e)]
        if cls.capabilities.chats and not cls.capabilities.chat_write:
            caps = [f"{c}(ro)" if c == "chats" else c for c in caps]
        tbl.add_row(
            name,
            cls.client_kind,
            "[green]enabled[/green]" if tcfg.enabled else "[dim]disabled[/dim]",
            tcfg.path or "-",
            ", ".join(caps),
        )
    console.print(tbl)


@tools_app.command("enable")
def cmd_tools_enable(
    tool: str = typer.Argument(..., help="Tool identifier."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
) -> None:
    """Enable a tool for synchronization."""
    registry = get_registry()
    if tool not in registry.list_names():
        console.print(f"[red]Unknown tool '{tool}'. Available: {', '.join(registry.list_names())}[/red]")
        raise typer.Exit(code=2)
    mgr, cfg = _load_effective_config(config_file)
    if tool not in cfg.tools:
        disc = discover_tools().get(tool)
        cfg.tools[tool] = ToolConfig(
            enabled=True,
            path=disc.root_path if disc else None,
            extra_paths=dict(disc.extra_paths) if disc else {},
            entities=list(disc.supported_entities) if disc else list(ALL_ENTITIES),
        )
    else:
        cfg.tools[tool].enabled = True
    mgr.save(cfg)
    console.print(f"[green]Enabled tool '{tool}' in {mgr.config_path}[/green]")


@tools_app.command("disable")
def cmd_tools_disable(
    tool: str = typer.Argument(..., help="Tool identifier to disable."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
) -> None:
    """Disable a tool from participating in synchronization."""
    registry = get_registry()
    if tool not in registry.list_names():
        console.print(f"[red]Unknown tool '{tool}'. Available: {', '.join(registry.list_names())}[/red]")
        raise typer.Exit(code=2)
    mgr, cfg = _load_effective_config(config_file)
    if tool not in cfg.tools:
        cfg.tools[tool] = ToolConfig(enabled=False)
    else:
        cfg.tools[tool].enabled = False
    mgr.save(cfg)
    console.print(f"[yellow]Disabled tool '{tool}' in {mgr.config_path}[/yellow]")


@tools_app.command("set-path")
def cmd_tools_set_path(
    tool: str = typer.Argument(..., help="Tool identifier."),
    path: Path = typer.Argument(..., help="Custom root config directory path."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
) -> None:
    """Override the auto-detected config root path for a tool."""
    registry = get_registry()
    if tool not in registry.list_names():
        console.print(f"[red]Unknown tool '{tool}'. Available: {', '.join(registry.list_names())}[/red]")
        raise typer.Exit(code=2)
    mgr, cfg = _load_effective_config(config_file)
    if tool not in cfg.tools:
        cfg.tools[tool] = ToolConfig(enabled=True, path=str(path))
    else:
        cfg.tools[tool].path = str(path)
    mgr.save(cfg)
    console.print(f"[green]Set path for '{tool}' to '{path}' in {mgr.config_path}[/green]")


@tools_app.command("set-entities")
def cmd_tools_set_entities(
    tool: str = typer.Argument(..., help="Tool identifier."),
    entities: str = typer.Argument(..., help="Comma-separated entity list."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
) -> None:
    """Configure which entity types sync for a specific tool."""
    registry = get_registry()
    if tool not in registry.list_names():
        console.print(f"[red]Unknown tool '{tool}'. Available: {', '.join(registry.list_names())}[/red]")
        raise typer.Exit(code=2)
    parsed: list[EntityType] = []
    for raw in entities.split(","):
        tok = raw.strip().lower()
        if not tok:
            continue
        if tok not in ALL_ENTITIES:
            console.print(f"[red]Invalid entity '{tok}'. Valid: {', '.join(ALL_ENTITIES)}[/red]")
            raise typer.Exit(code=2)
        parsed.append(tok)  # type: ignore[arg-type]
    mgr, cfg = _load_effective_config(config_file)
    if tool not in cfg.tools:
        cfg.tools[tool] = ToolConfig(enabled=True, entities=parsed)
    else:
        cfg.tools[tool].entities = parsed
    mgr.save(cfg)
    console.print(f"[green]Updated entities for '{tool}' -> {', '.join(parsed)}[/green]")


# --- Patch, Handoff, Workspace, Board, Skills Commands ---


@app.command("patch")
def cmd_patch(
    auto: bool = typer.Option(False, "--auto", help="Run in Auto-Patch mode (preflight + backup + repair + sync)."),
    manual: bool = typer.Option(False, "--manual", help="Run in Manual Patch mode (preview diff before applying)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview patch without modifying disk."),
    targets: str | None = typer.Option(None, "--targets", help="Comma-separated target tools (others untouched)."),
    include: list[str] = typer.Option([], "--include", help="Include selector(s), e.g. skills:*,mcp."),
    exclude: list[str] = typer.Option([], "--exclude", help="Exclude selector(s), e.g. mcp:internal-*."),
    scope: SyncScope = typer.Option("all", "--scope", help="Sync scope: user, project, or all."),
    prune: bool = typer.Option(False, "--prune", help="Opt-in mirror mode deleting extras absent from canonical set."),
    strip_secrets: bool = typer.Option(False, "--strip-secrets", help="Replace literal secrets with env-var placeholders."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every individual skill entry."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Auto-Patch or Manual Patch checked/targeted AI apps with backup, repair, sync, and Apply Ledger."""
    _, cfg = _load_effective_config(config_file, data_dir)
    mode: PatchMode = "auto" if auto else ("manual" if manual else cfg.patch_mode)
    if targets:
        selected = [x.strip() for x in targets.split(",") if x.strip()]
    else:
        selected = [name for name, tc in cfg.tools.items() if tc.enabled]

    if not selected:
        console.print("[yellow]No target tools enabled or selected. Run 'agentsync discover' first.[/yellow]")
        raise typer.Exit(code=1)

    opts = SyncOptions(
        dry_run=dry_run,
        force=force,
        scope=scope,
        prune=prune,
        strip_secrets=strip_secrets,
        strict_vocab_guard=cfg.strict_vocab_guard,
        skill_link_mode=cfg.skill_link_mode,
        include=include,
        exclude=exclude,
        targets=selected,
    )
    engine = PatchEngine(cfg)
    result = engine.run_patch(
        selected_tools=selected,
        mode=mode,
        dry_run=dry_run,
        options=opts,
    )
    _render_plan(result.sync_plan, show_all=verbose)


@app.command("handoff")
def cmd_handoff(
    source_tool: str = typer.Option(..., "--from", help="Source tool containing the chat session."),
    target_tool: str = typer.Option(..., "--to", help="Target tool to receive the conversation + context."),
    session: str | None = typer.Option(None, "--session", help="Session/chat ID prefix (defaults to latest)."),
    open_mode: LaunchMode = typer.Option("none", "--open", help="Open target after handoff: none, auto, app, terminal."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview handoff without modifying disk."),
    force: bool = typer.Option(False, "--force", help="Ignore running app lock guard on target tool."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Transfer a live chat session with project path, rules, and skills to another AI agent with DB verification."""
    _, cfg = _load_effective_config(config_file, data_dir)
    reg = get_registry()
    adapters = {
        name: reg.create(name, tc)
        for name, tc in cfg.tools.items()
        if name in reg.list_names() and tc.path
    }
    eng = HandoffEngine(cfg, adapters)
    try:
        if dry_run:
            prev = eng.preview(source_tool, target_tool, session_id=session)
            console.print(
                Panel(
                    f"Source: {prev.source_tool} ({prev.session_id})\n"
                    f"Target: {prev.target_tool}\n"
                    f"Title: {prev.title}\n"
                    f"Project: {prev.project_path or '(none)'}\n"
                    f"Messages: {prev.message_count} | Skills: {prev.active_skills_count} | "
                    f"Project rules: {'yes' if prev.has_project_instructions else 'no'}\n"
                    f"Reusable target thread: {prev.reusable_target_id or 'new'}",
                    title="[yellow]Handoff Preview (DRY-RUN)[/yellow]",
                )
            )
            return
        res = eng.execute(
            source_tool,
            target_tool,
            session_id=session,
            open_mode=open_mode,
            force=force,
        )
    except (AppsRunningError, ValueError, RuntimeError) as exc:
        console.print(f"[bold red]Handoff failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    badge = "[cyan]REUSED EXISTING THREAD[/cyan]" if res.reused_existing else "[green]VERIFIED ON DISK[/green]"
    console.print(
        f"{badge}: {res.source_tool} ({res.source_session_id}) -> {res.target_tool} ({res.target_session_id}) "
        f"[{res.verified_message_count} messages, +{res.synced_skills} skills]"
    )


@workspace_app.command("init")
def cmd_workspace_init(
    path: Path = typer.Option(Path("."), "--path", help="Project root directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview workspace.yaml creation."),
) -> None:
    """Initialize `.agentsync/workspace.yaml` unified workspace manifest."""
    eng = WorkspaceEngine()
    manifest, mpath = eng.init_workspace(path.resolve(), dry_run=dry_run)
    prefix = "[yellow](DRY-RUN)[/yellow] Would create" if dry_run else "[green]Initialized[/green]"
    console.print(f"{prefix} workspace manifest '{manifest.name}' at {mpath}")


@workspace_app.command("sync")
def cmd_workspace_sync(
    path: Path = typer.Option(Path("."), "--path", help="Project root directory."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview unified instruction sync."),
) -> None:
    """Synchronize AGENTS.md, CLAUDE.md, GEMINI.md, .rules, and Copilot instructions in a workspace."""
    eng = WorkspaceEngine()
    res = eng.sync_workspace(path.resolve(), dry_run=dry_run)
    badge = "[yellow](DRY-RUN)[/yellow]" if dry_run else "[green](APPLIED)[/green]"
    console.print(
        f"Workspace sync {badge}: updated={len(res.updated_files)} ({', '.join(res.updated_files) or '-'}), "
        f"unchanged={len(res.unchanged_files)}"
    )


@app.command("board")
def cmd_board(
    output: Path | None = typer.Option(None, "--output", "-o", help="Output HTML file path."),
    open_browser: bool = typer.Option(False, "--open", help="Open generated board in default browser."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Generate a self-contained offline HTML Visual Inventory Board across all enabled tools."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    out = output or (cfg.resolved_data_dir() / "inventory-board.html")
    path, count = generate_inventory_board(adapters, out)
    console.print(f"[green]Generated offline HTML Inventory Board ({count} entities):[/green] {path}")
    if open_browser:
        webbrowser.open(path.as_uri())


@app.command("list-skills")
def cmd_list_skills(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON array."),
    tool: str | None = typer.Option(None, "--tool", help="Filter by a single tool name."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """List skills discovered across enabled AI tools (scriptable with --json)."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg, targets=[tool] if tool else None)
    rows: list[dict[str, str]] = []
    for a in adapters:
        if not a.capabilities.skills:
            continue
        for name, sk in sorted(a.read_skills().items()):
            rows.append(
                {
                    "tool": a.tool_name,
                    "name": name,
                    "description": sk.description,
                    "content_hash": sk.content_hash[:12],
                }
            )
    if as_json:
        typer.echo(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    tbl = Table(title=f"Discovered Skills ({len(rows)})", expand=True)
    tbl.add_column("Tool", style="bold", no_wrap=True)
    tbl.add_column("Skill", no_wrap=True)
    tbl.add_column("Hash", no_wrap=True)
    tbl.add_column("Description", overflow="fold", ratio=2)
    for r in rows:
        tbl.add_row(r["tool"], r["name"], r["content_hash"], r["description"])
    console.print(tbl)


@app.command("install-skill")
def cmd_install_skill(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview which tools would receive agentsync/SKILL.md."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Install the built-in lightweight `agentsync` SKILL.md into enabled AI coding tools."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    installed = install_agentsync_skill(adapters, dry_run=dry_run)
    prefix = "[yellow](DRY-RUN) Would install[/yellow]" if dry_run else "[green]Installed[/green]"
    console.print(f"{prefix} agentsync/SKILL.md into {len(installed)} tool(s): {', '.join(installed)}")


# --- Original Operational Commands (100% Backward Compatible) ---


@app.command("audit")
def cmd_audit(
    include_subagents: bool = typer.Option(False, "--include-subagents", help="Include subagent chats."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Inspect paths, running processes, and entity counts across all enabled tools (read-only)."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)

    running = detect_running_apps(check_apps=[a.tool_name for a in adapters])
    proc_table = Table(title="Running Processes (Write Lock Guard)")
    proc_table.add_column("Tool", style="bold")
    proc_table.add_column("Processes", justify="right")
    proc_table.add_column("Sample Executables")
    for a in adapters:
        matched = [p for p in running if p.app == a.tool_name]
        sample = ", ".join(sorted({p.name for p in matched})) or "-"
        cnt = f"[red]{len(matched)}[/red]" if matched else "[green]0 (closed)[/green]"
        proc_table.add_row(a.tool_name, cnt, sample)
    console.print(proc_table)

    tbl = Table(title="Multi-Tool Entity Audit (Read-Only)")
    tbl.add_column("Tool", style="bold")
    tbl.add_column("Skills", justify="right")
    tbl.add_column("MCP", justify="right")
    tbl.add_column("Plugins", justify="right")
    tbl.add_column("Global Rules", justify="right")
    tbl.add_column("Projects", justify="right")
    tbl.add_column("Chats", justify="right")

    for a in adapters:
        projs = a.read_projects() if a.capabilities.projects else {}
        dups = sum(p.duplicate_count(a.tool_name) for p in projs.values())
        skills = a.read_skills() if a.capabilities.skills else {}
        mcps = a.read_mcp_servers() if a.capabilities.mcp else {}
        plugins = a.read_plugins() if a.capabilities.plugins else {}
        gdoc = a.read_global_instructions() if a.capabilities.instructions else None
        chats = (
            a.read_chats(projs, include_subagents=include_subagents) if a.capabilities.chats else []
        )
        proj_str = f"{len(projs)} (+{dups} dup)" if dups else str(len(projs))
        tbl.add_row(
            a.tool_name,
            str(len(skills)) if a.capabilities.skills else "-",
            str(len(mcps)) if a.capabilities.mcp else "-",
            str(len(plugins)) if a.capabilities.plugins else "-",
            f"{len(gdoc.raw_text.encode('utf-8'))} B" if gdoc else "0 B",
            proj_str if a.capabilities.projects else "-",
            str(len(chats)) if a.capabilities.chats else "-",
        )
    console.print(tbl)


@app.command("doctor")
def cmd_doctor(
    fix: bool = typer.Option(False, "--fix", help="Quarantine and repair fixable issues automatically."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview repairs without modifying disk."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard when fixing."),
    verify_backups: bool = typer.Option(
        False,
        "--verify-backups",
        help="Also run SHA-256 and SQLite PRAGMA integrity_check on stored backups.",
    ),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Validate configs, SQLite/protobuf stores, autostart integrity, daemon health, and split installs."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    diag = DiagnosticsEngine(cfg, adapters, config_path=mgr.config_path)
    report = diag.run_all(verify_backups=verify_backups)

    auto_state = (
        "enabled (healthy)"
        if (report.autostart_enabled and report.autostart_healthy)
        else ("enabled (UNHEALTHY)" if report.autostart_enabled else "disabled")
    )
    daemon_state = "running" if report.daemon_running else "stopped"
    console.print(
        f"[dim]Runtime Engine: {report.runtime_engine} | Autostart: {auto_state} | "
        f"Daemon: {daemon_state} | Backups: {report.backup_count} | "
        f"Pending repairs: {report.pending_repairs}[/dim]"
    )

    issues = report.issues
    if not issues:
        console.print("[green]Doctor check passed: 0 issues detected across all enabled tools.[/green]")
        return

    tbl = Table(title=f"Doctor Diagnostics ({len(issues)} issue(s) found)", expand=True)
    tbl.add_column("Severity", no_wrap=True)
    tbl.add_column("Tool", style="bold", no_wrap=True)
    tbl.add_column("Code", overflow="fold", ratio=1)
    tbl.add_column("Fix", no_wrap=True)
    tbl.add_column("Details", overflow="fold", ratio=2)

    sev_color = {"error": "bold red", "warning": "yellow", "info": "cyan"}
    for iss in issues:
        c = sev_color.get(iss.severity, "white")
        tbl.add_row(
            f"[{c}]{iss.severity.upper()}[/{c}]",
            iss.tool,
            iss.code,
            "[green]yes[/green]" if iss.fixable else "no",
            mask_text(f"{iss.message} ({iss.path})" if iss.path else iss.message),
        )
    console.print(tbl)

    if fix:
        # 1. Repair agentsync-internal issues (legacy autostart, stale daemon lock)
        internal_codes = {i.code for i in issues if i.tool == "agentsync" and i.fixable}
        if "AUTOSTART_LEGACY_OR_BROKEN" in internal_codes:
            purged = purge_legacy_autostart_artifacts(cfg, dry_run=dry_run)
            for line in purged:
                console.print(f"[green]agentsync:[/green] {mask_text(line)}")
            if cfg.autostart_enabled and not dry_run:
                try:
                    st = enable_autostart(cfg, config_path=mgr.config_path, data_dir=data_dir)
                    console.print(f"[green]agentsync:[/green] Re-registered native autostart ({st.executable})")
                except Exception as exc:
                    console.print(f"[yellow]agentsync:[/yellow] Autostart re-registration note: {exc}")
        if "DAEMON_STALE_LOCK" in internal_codes and cfg.daemon_lock_path.exists():
            if dry_run:
                console.print(f"[yellow]agentsync:[/yellow] Would remove stale lock {cfg.daemon_lock_path}")
            else:
                cfg.daemon_lock_path.unlink(missing_ok=True)
                console.print("[green]agentsync:[/green] Removed stale daemon lockfile.")

        # 2. Repair tool-specific adapter issues (SQLite DBs, rollouts, configs, indexes)
        doc_engine = DoctorEngine(cfg, adapters)
        try:
            results, backup_id = doc_engine.repair(dry_run=dry_run, force=force)
        except AppsRunningError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
            console.print(
                "[yellow]Tip: run 'agentsync repair --on-close' to auto-heal safely 2.5s after apps exit.[/yellow]"
            )
            raise typer.Exit(code=1) from exc
        if backup_id:
            console.print(f"[cyan]Pre-repair backup created: {backup_id}[/cyan]")
        for res in results:
            console.print(
                f"[green]{res.tool}: {'would repair' if dry_run else 'repaired'} {res.fixed_count} item(s)[/green]"
            )
            for line in res.details[:15]:
                console.print(f"  - {mask_text(line)}")
            if len(res.details) > 15:
                console.print(f"  ... (+{len(res.details) - 15} more)")


@app.command("repair")
def cmd_repair(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview repairs without writing to disk."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard."),
    on_close: bool = typer.Option(
        False,
        "--on-close",
        help="Queue fixable issues in repair_queue.json so the daemon auto-heals 2.5s after apps exit.",
    ),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Quarantine and regenerate broken rollouts, symlinks, autostart tasks, and out-of-sync indexes."""
    if on_close:
        mgr, cfg = _load_effective_config(config_file, data_dir)
        adapters = _build_adapters(cfg)
        diag = DiagnosticsEngine(cfg, adapters, config_path=mgr.config_path)
        report = diag.run_all(verify_backups=False)
        fixable = [i for i in report.issues if i.fixable]
        if not fixable:
            console.print("[green]No fixable issues to queue for repair on close.[/green]")
            return
        if dry_run:
            console.print(
                f"[yellow](DRY-RUN) Would queue {len(fixable)} fixable issue(s) to {cfg.repair_queue_path}:[/yellow]"
            )
            for iss in fixable:
                console.print(f"  - [{iss.tool}] {iss.code}: {mask_text(iss.message)}")
            return
        queue = diag.queue_repairs_on_close(fixable)
        notified = send_daemon_command(cfg, "repair_queued")
        console.print(
            f"[green]Queued {len(queue.entries)} repair(s) in {cfg.repair_queue_path}.[/green]"
        )
        if notified:
            console.print(
                "[cyan]Background daemon notified: repairs will execute automatically 2.5s after locking apps close.[/cyan]"
            )
        else:
            console.print(
                "[yellow]Background daemon is currently stopped. Start it with 'agentsync daemon start' "
                "or run 'agentsync repair' once locking apps are closed.[/yellow]"
            )
        return
    cmd_doctor(fix=True, dry_run=dry_run, force=force, config_file=config_file, data_dir=data_dir)


@app.command("bootstrap")
def cmd_bootstrap(
    source_tool: str = typer.Option(..., "--from", help="Authoritative source tool for initial migration."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff plan without modifying any files."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard."),
    include_subagents: bool = typer.Option(False, "--include-subagents", help="Include subagent chats."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every individual skill entry."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Initial clean migration: backup -> clear target skills/MCP/plugins -> import -> merge projects/rules/chats."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    options = SyncOptions(dry_run=dry_run, force=force, include_subagents=include_subagents)
    engine = BootstrapEngine(cfg, adapters, options=options)

    if dry_run:
        running = detect_running_apps(check_apps=[a.tool_name for a in adapters])
        if running:
            apps_str = ", ".join(sorted({p.app for p in running}))
            console.print(
                f"[yellow]Notice: {apps_str} currently running. Read-only --dry-run is safe, "
                "but close running apps before applying.[/yellow]"
            )

    try:
        plan = engine.run_from(source_tool=source_tool, dry_run=dry_run, force=force)
    except (AppsRunningError, ValueError) as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_plan(plan, show_all=verbose)


@app.command("sync")
def cmd_sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff plan without modifying any files."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard."),
    include_subagents: bool = typer.Option(False, "--include-subagents", help="Include subagent chats."),
    backup: bool | None = typer.Option(
        None,
        "--backup/--no-backup",
        help="Automatically create a SHA-256 + SQLite verified backup before mutating (defaults to config auto_backup).",
    ),
    targets: str | None = typer.Option(None, "--targets", help="Comma-separated target tools."),
    include: list[str] = typer.Option([], "--include", help="Include selectors, e.g. skills:*,mcp."),
    exclude: list[str] = typer.Option([], "--exclude", help="Exclude selectors, e.g. mcp:internal-*."),
    scope: SyncScope = typer.Option("all", "--scope", help="Sync scope: user, project, or all."),
    prune: bool = typer.Option(False, "--prune", help="Opt-in mirror mode deleting extras absent from canonical set."),
    strip_secrets: bool = typer.Option(False, "--strip-secrets", help="Replace literal secrets with env placeholders."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every individual skill entry."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Idempotent bidirectional synchronization across all enabled AI tools."""
    _, cfg = _load_effective_config(config_file, data_dir)
    target_list = [x.strip() for x in targets.split(",") if x.strip()] if targets else []
    adapters = _build_adapters(cfg, targets=target_list or None)
    options = SyncOptions(
        dry_run=dry_run,
        force=force,
        include_subagents=include_subagents,
        scope=scope,
        prune=prune,
        strip_secrets=strip_secrets,
        strict_vocab_guard=cfg.strict_vocab_guard,
        skill_link_mode=cfg.skill_link_mode,
        include=include,
        exclude=exclude,
        targets=target_list,
    )
    engine = SyncEngine(cfg, adapters, options=options)
    effective_backup = cfg.auto_backup if backup is None else backup

    try:
        plan = engine.run(dry_run=dry_run, force=force, create_backup=effective_backup)
    except AppsRunningError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_plan(plan, show_all=verbose)


@app.command("watch")
def cmd_watch(
    debounce: float | None = typer.Option(
        None,
        "--debounce",
        help="Debounce quiet interval in seconds (defaults to active power profile).",
    ),
    force: bool = typer.Option(False, "--force", help="Allow writes while target apps are running."),
    daemon: bool = typer.Option(False, "--daemon", help="Run silently with rotating daemon.log and Sync-on-Close."),
    include_subagents: bool = typer.Option(False, "--include-subagents", help="Include subagent chats."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Run continuous Split-Tier real-time file watching with automatic Sync-on-Close."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    eff_debounce = debounce if debounce is not None else cfg.effective_debounce_seconds()
    options = SyncOptions(
        dry_run=False,
        force=force,
        include_subagents=include_subagents,
        debounce_seconds=eff_debounce,
    )

    def _on_done(plan: SyncPlan) -> None:
        if daemon:
            return
        if plan.is_empty:
            console.print("[dim]watch: no changes detected[/dim]")
        else:
            console.print(
                f"[green]watch: applied {len(plan.mutating_actions)} action(s) "
                f"(conflicts={len(plan.conflict_actions)}, skipped={len(plan.skipped_actions)}, "
                f"backup={plan.backup_id or '-'})[/green]"
            )

    def _on_err(exc: Exception) -> None:
        if not daemon:
            console.print(f"[yellow]watch: {mask_text(str(exc))}[/yellow]")

    def _on_log(level: str, msg: str) -> None:
        if not daemon:
            color = "yellow" if level == "warning" else ("red" if level == "error" else "dim")
            console.print(f"[{color}]watch: {mask_text(msg)}[/{color}]")

    if not daemon:
        console.print(
            f"[cyan]Starting Split-Tier watch across {', '.join(a.tool_name for a in adapters)} "
            f"(profile={cfg.power_profile}, debounce={eff_debounce}s, Sync-on-Close={'on' if cfg.sync_on_close_enabled else 'off'}). "
            "Press Ctrl+C to stop.[/cyan]"
        )
    watcher = SyncWatcher(
        cfg,
        adapters,
        options=options,
        on_sync_done=_on_done,
        on_error=_on_err,
        on_log=_on_log,
        daemon_mode=daemon,
        run_startup_sync=True,
    )
    watcher.run_forever()


# --- Native Daemon Control, Live Config, Autostart & Self-Updater ---


@daemon_app.command("status")
def cmd_daemon_status(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Display live telemetry for the single-instance background daemon."""
    _, cfg = _load_effective_config(config_file, data_dir)
    st = read_daemon_status(cfg)
    queue = load_repair_queue(cfg.repair_queue_path)
    badge = "[green]" + st.status.upper() + "[/green]" if st.status != "stopped" else "[yellow]STOPPED[/yellow]"
    console.print(
        Panel(
            f"State          : {badge}\n"
            f"PID / Version  : {st.pid or '-'} (v{st.version})\n"
            f"Runtime Engine : {st.runtime_engine}\n"
            f"Power Profile  : {st.power_profile}\n"
            f"CPU / RAM      : {st.cpu_percent:.1f}% / {st.rss_mb:.1f} MB\n"
            f"Syncs / Pending: {st.sync_count} completed | {st.pending_sync_count} pending\n"
            f"Repair Queue   : {len(queue.entries)} queued\n"
            f"Locked Apps    : {', '.join(st.locked_apps) or 'none'}\n"
            f"Last Sync      : {st.last_sync_utc or '-'}\n"
            f"Log File       : {cfg.daemon_log_path}\n"
            f"Last Error     : {st.last_error or 'none'}",
            title="AgentSync Background Daemon",
        )
    )


@daemon_app.command("start")
def cmd_daemon_start(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Start the windowless single-instance background daemon (agentsyncw)."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    st = start_daemon_background(cfg, config_path=mgr.config_path, data_dir=data_dir)
    if st.status != "stopped" and st.pid > 0:
        console.print(f"[green]Daemon running:[/green] PID {st.pid} (profile={st.power_profile}, engine={st.runtime_engine})")
    else:
        console.print(f"[yellow]Daemon spawn initiated; check log at {cfg.daemon_log_path}.[/yellow]")


@daemon_app.command("stop")
def cmd_daemon_stop(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Stop the running background daemon gracefully."""
    _, cfg = _load_effective_config(config_file, data_dir)
    stopped = stop_daemon(cfg)
    if stopped:
        console.print("[green]Background daemon stopped cleanly.[/green]")
    else:
        console.print("[yellow]Background daemon was not running.[/yellow]")


@daemon_app.command("restart")
def cmd_daemon_restart(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Restart the single-instance background daemon."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    st = restart_daemon(cfg, config_path=mgr.config_path, data_dir=data_dir)
    console.print(f"[green]Daemon restarted:[/green] status={st.status}, PID={st.pid or '-'}")


@daemon_app.command("pause")
def cmd_daemon_pause(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Pause background real-time syncing without terminating the daemon."""
    _, cfg = _load_effective_config(config_file, data_dir)
    if send_daemon_command(cfg, "pause"):
        console.print("[yellow]Sent PAUSE signal to background daemon.[/yellow]")
    else:
        console.print("[yellow]Background daemon is not running.[/yellow]")


@daemon_app.command("resume")
def cmd_daemon_resume(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Resume a paused background daemon."""
    _, cfg = _load_effective_config(config_file, data_dir)
    if send_daemon_command(cfg, "resume"):
        console.print("[green]Sent RESUME signal to background daemon.[/green]")
    else:
        console.print("[yellow]Background daemon is not running.[/yellow]")


@daemon_app.command("run")
def cmd_daemon_run(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Run the single-instance daemon loop in the current process (used by systemd/launchd/fallback)."""
    code = run_daemon_loop(config_path=config_file, data_dir=data_dir)
    if code != 0:
        raise typer.Exit(code=code)


@config_app.command("list")
def cmd_config_list(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """List all editable AgentSync settings and their current values."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    tbl = Table(title=f"AgentSync Settings ({mgr.config_path})", expand=True)
    tbl.add_column("Key", style="bold cyan", no_wrap=True)
    tbl.add_column("Value", overflow="fold")
    tbl.add_column("Type", style="dim", no_wrap=True)
    for key in EDITABLE_CONFIG_KEYS:
        val = getattr(cfg, key)
        tbl.add_row(key, str(val), type(val).__name__)
    console.print(tbl)


@config_app.command("get")
def cmd_config_get(
    key: str = typer.Argument(..., help="Configuration key name."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Read a single AgentSync configuration setting."""
    _, cfg = _load_effective_config(config_file, data_dir)
    if key not in EDITABLE_CONFIG_KEYS:
        console.print(f"[red]Unknown setting '{key}'. Valid keys: {', '.join(EDITABLE_CONFIG_KEYS)}[/red]")
        raise typer.Exit(code=2)
    typer.echo(str(getattr(cfg, key)))


@config_app.command("set")
def cmd_config_set(
    key: str = typer.Argument(..., help="Configuration key name."),
    value: str = typer.Argument(..., help="New setting value."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Update a setting in config.toml and live hot-reload the running daemon."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    try:
        updated = set_config_setting(cfg, key, value)
    except Exception as exc:
        console.print(f"[bold red]Invalid setting update:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    mgr.save(updated)
    reloaded = send_daemon_command(updated, "reload")
    reload_note = " (live daemon hot-reloaded)" if reloaded else ""
    console.print(f"[green]Updated {key} = {getattr(updated, key)}[/green]{reload_note}")


@autostart_app.command("status")
def cmd_autostart_status(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Inspect whether native windowless background autostart is enabled and healthy."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    st = autostart_status(cfg, config_path=mgr.config_path, data_dir=data_dir)
    badge = "[green]ENABLED[/green]" if st.enabled else "[yellow]DISABLED[/yellow]"
    health = "[green]HEALTHY[/green]" if st.healthy else "[bold red]UNHEALTHY[/bold red]"
    lines = [
        f"State     : {badge} | Health: {health}",
        f"Platform  : {st.platform} ({st.mechanism})",
        f"Artifact  : {st.artifact_path}",
        f"Executable: {st.executable or '-'}",
        f"Arguments : {st.arguments or '-'}",
        f"Daemon log: {cfg.daemon_log_path}",
        f"Details   : {st.detail}",
    ]
    if st.issues:
        lines.append("Issues    :")
        lines.extend(f"  - [bold red]{iss}[/bold red]" for iss in st.issues)
    console.print(Panel("\n".join(lines), title="AgentSync Native Autostart Status"))


@autostart_app.command("enable")
def cmd_autostart_enable(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Enable native windowless background synchronization on login (ZERO VBS/WSH, no Admin required)."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    try:
        st = enable_autostart(cfg, config_path=mgr.config_path, data_dir=data_dir)
    except Exception as exc:
        console.print(f"[bold red]Failed to enable autostart:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    cfg.autostart_enabled = True
    mgr.save(cfg)
    console.print(f"[green]Native autostart enabled ({st.mechanism}):[/green] {st.detail}")
    console.print(f"[dim]Executable: {st.executable} {st.arguments} | Log: {cfg.daemon_log_path}[/dim]")


@autostart_app.command("disable")
def cmd_autostart_disable(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Disable and remove unattended background autostart plus any legacy launcher artifacts."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    st = disable_autostart(cfg)
    cfg.autostart_enabled = False
    mgr.save(cfg)
    console.print(f"[yellow]Autostart disabled ({st.mechanism}):[/yellow] {st.detail}")


@app.command("update")
def cmd_update(
    check: bool = typer.Option(False, "--check", help="Check for newer releases without installing."),
    force: bool = typer.Option(False, "--force", help="Bypass 6-hour update cache and force upgrade."),
    pre: bool = typer.Option(False, "--pre", help="Include pre-release versions."),
    detached: bool = typer.Option(
        False,
        "--detached",
        help="On Windows, hand off the upgrade to a detached PowerShell helper after exit to prevent WinError 32.",
    ),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Check for newer AgentSync releases on GitHub and self-update safely without WinError 32."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    want_pre = pre or cfg.update_channel == "prerelease"
    info = check_for_updates(
        cfg.update_cache_path,
        force=(force or check),
        include_pre=want_pre,
        config=cfg,
    )
    if info.error:
        console.print(f"[yellow]Update check failed (offline or rate-limited):[/yellow] {info.error}")
        console.print(f"[dim]Current version: v{info.current_version} (install method: {info.install_method})[/dim]")
        if check:
            return
    elif info.update_available:
        console.print(
            f"[green]Update available:[/green] v{info.current_version} -> [bold cyan]v{info.latest_version}[/bold cyan] "
            f"(install method: {info.install_method})"
        )
        console.print(f"[dim]Release: {info.release_url}[/dim]")
    else:
        console.print(
            f"[green]AgentSync is up to date[/green] (v{info.current_version}, install method: {info.install_method})."
        )
        if not force or check:
            return

    if check:
        return

    invoked_as_exe = sys.platform == "win32" and Path(sys.argv[0]).name.lower() in (
        "agentsync.exe",
        "agentsyncw.exe",
    )
    use_detached = detached or invoked_as_exe

    mode_note = "detached Windows helper" if use_detached else info.install_method
    console.print(f"[cyan]Running upgrade via {mode_note}...[/cyan]")
    res = perform_update(
        config=cfg,
        method=info.install_method,
        include_pre=want_pre,
        detached_on_windows=use_detached,
        config_path=mgr.config_path,
    )
    if res.success:
        if res.detached_handoff:
            console.print(f"[green]{res.restart_notice}[/green]")
            raise typer.Exit(code=0)
        console.print(
            f"[green]Upgrade completed:[/green] v{res.previous_version} -> v{res.new_version or info.latest_version}"
        )
        if res.restart_notice:
            console.print(f"[yellow]{res.restart_notice}[/yellow]")
    else:
        console.print(f"[bold red]Upgrade failed:[/bold red] {res.error}")
        if res.restart_notice:
            console.print(f"[yellow]{res.restart_notice}[/yellow]")
        raise typer.Exit(code=1)


@bundle_app.command("export")
def cmd_bundle_export(
    output: Path = typer.Argument(..., help="Output .zip path for the Portable Profile Bundle."),
    include_secrets: bool = typer.Option(
        False,
        "--include-secrets",
        help="Include literal secret values (default redacts all literal secrets to ${ENV_VAR}).",
    ),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Export canonical skills, MCP servers, and global instructions to a SHA-256 verified .zip bundle."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    mf = export_profile_bundle(output, adapters, include_secrets=include_secrets)
    redacted_str = "secrets REDACTED" if mf.secrets_redacted else "secrets INCLUDED"
    console.print(
        f"[green]Exported Portable Profile Bundle ({redacted_str}):[/green] {output}\n"
        f"  Skills: {mf.skill_count} | MCP: {mf.mcp_count} | Global rules: {'yes' if mf.has_global_instructions else 'no'} | "
        f"Verified entries: {len(mf.sha256_by_entry)}"
    )


@bundle_app.command("import")
def cmd_bundle_import(
    bundle_zip: Path = typer.Argument(..., help="Input Portable Profile Bundle .zip file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Verify SHA-256 manifest and preview import without writes."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard."),
    targets: str | None = typer.Option(None, "--targets", help="Comma-separated target tools."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Verify SHA-256 checksums and import a Portable Profile Bundle into enabled AI tools."""
    _, cfg = _load_effective_config(config_file, data_dir)
    target_list = [x.strip() for x in targets.split(",") if x.strip()] if targets else None
    adapters = _build_adapters(cfg, targets=target_list)
    try:
        res = import_profile_bundle(bundle_zip, cfg, adapters, dry_run=dry_run, force=force)
    except (AppsRunningError, FileNotFoundError, ValueError) as exc:
        console.print(f"[bold red]Bundle import failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    badge = "[yellow](DRY-RUN)[/yellow]" if res.dry_run else "[green](IMPORTED)[/green]"
    console.print(
        f"Bundle import {badge}: verified={res.verified_entries} entries, skills={res.skills_imported}, "
        f"mcp={res.mcp_imported}, rules={'yes' if res.instructions_imported else 'no'}, "
        f"targets={', '.join(res.target_tools)}" + (f" [backup={res.backup_id}]" if res.backup_id else "")
    )


@app.command("support-bundle")
def cmd_support_bundle(
    output: Path | None = typer.Option(None, "--output", "-o", help="Output .zip path."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Create a privacy-safe anonymized diagnostics .zip (zero chats, secrets, emails, or usernames)."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    out = output or (cfg.resolved_data_dir() / "agentsync-support-bundle.zip")
    path = create_support_bundle(out, cfg, adapters)
    console.print(
        f"[green]Created anonymized support bundle:[/green] {path}\n"
        "[dim]Strictly stripped: API keys, bearer tokens, emails, OS usernames, and all chat contents.[/dim]"
    )


@backup_app.command("create")
def cmd_backup_create(
    reason: str = typer.Option("manual", "--reason", help="Backup label/reason."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Create a manual SHA-256 + SQLite verified backup snapshot immediately."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    mf = BackupManager(cfg).create_backup(adapters, reason=reason)
    console.print(
        f"[green]Created verified backup snapshot:[/green] {mf.backup_id} "
        f"({len(mf.entries)} files across {', '.join(mf.tools)})"
    )


@backup_app.command("verify")
def cmd_backup_verify(
    backup_id: str | None = typer.Option(None, "--id", help="Verify a single backup ID (defaults to all)."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Verify SHA-256 digests and SQLite PRAGMA integrity_check across stored backup snapshots."""
    _, cfg = _load_effective_config(config_file, data_dir)
    bmgr = BackupManager(cfg)
    reports = [bmgr.verify_backup(backup_id)] if backup_id else bmgr.verify_all_backups()
    if not reports:
        console.print("[yellow]No backups found to verify.[/yellow]")
        return
    tbl = Table(title=f"Backup Verification ({len(reports)} snapshot(s))", expand=True)
    tbl.add_column("Backup ID", style="cyan")
    tbl.add_column("Status", no_wrap=True)
    tbl.add_column("Files", justify="right")
    tbl.add_column("SQLite DBs", justify="right")
    tbl.add_column("Size (KB)", justify="right")
    tbl.add_column("Details", overflow="fold")
    failed = 0
    for r in reports:
        if not r.ok:
            failed += 1
        tbl.add_row(
            r.backup_id,
            "[green]OK[/green]" if r.ok else "[bold red]FAIL[/bold red]",
            str(r.checked_files),
            str(r.checked_sqlite),
            str(r.total_bytes // 1024),
            "ok" if r.ok else "; ".join(r.errors),
        )
    console.print(tbl)
    if failed:
        raise typer.Exit(code=1)


@app.command("status")
def cmd_status(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Show runtime engine, daemon telemetry, autostart health, synced entities, backups, and ledgers."""
    mgr, cfg = _load_effective_config(config_file, data_dir)
    d_st = read_daemon_status(cfg)
    a_st = autostart_status(cfg, config_path=mgr.config_path, data_dir=data_dir)
    split = detect_split_installations(cfg)
    r_queue = load_repair_queue(cfg.repair_queue_path)

    daemon_line = (
        f"[green]{d_st.status.upper()}[/green] (PID {d_st.pid}, {d_st.rss_mb:.1f} MB RAM, {d_st.cpu_percent:.1f}% CPU, "
        f"syncs={d_st.sync_count}, pending={d_st.pending_sync_count})"
        if d_st.status != "stopped"
        else "[yellow]STOPPED[/yellow]"
    )
    auto_line = (
        "[green]ENABLED (healthy)[/green]"
        if (a_st.enabled and a_st.healthy)
        else ("[bold red]ENABLED (UNHEALTHY)[/bold red]" if a_st.enabled else "[yellow]DISABLED[/yellow]")
    )
    split_line = (
        f"[yellow]DRIFT DETECTED: {split.summary}[/yellow]"
        if split.has_version_drift
        else "[green]OK (no version drift)[/green]"
    )
    console.print(
        Panel(
            f"Version        : v{__version__}\n"
            f"Runtime Engine : {runtime_engine_label(cfg.runtime_engine)}\n"
            f"Power Profile  : {cfg.power_profile} (debounce={cfg.effective_debounce_seconds()}s, "
            f"realtime={'on' if cfg.realtime_sync_enabled else 'off'}, "
            f"sync-on-close={'on' if cfg.sync_on_close_enabled else 'off'})\n"
            f"Daemon         : {daemon_line}\n"
            f"Autostart      : {auto_line}\n"
            f"Repair Queue   : {len(r_queue.entries)} pending\n"
            f"Install Check  : {split_line}",
            title="AgentSync Runtime & Background Engine",
        )
    )

    state = StateStore(cfg.state_db_path, readonly=True)
    try:
        counts = state.get_summary_counts()
        runs = state.list_runs(limit=5)
        conflicts = state.list_conflicts(limit=10)
    finally:
        state.close()

    tbl = Table(title=f"Sync State Summary ({cfg.state_db_path})")
    tbl.add_column("Entity", style="bold")
    tbl.add_column("Count", justify="right")
    for k, v in counts.items():
        tbl.add_row(k, str(v))
    console.print(tbl)

    bmgr = BackupManager(cfg)
    backups = bmgr.list_backups()
    if backups:
        btbl = Table(title="Available Backups (use 'agentsync restore <backup_id>')")
        btbl.add_column("Backup ID", style="cyan")
        btbl.add_column("Created (UTC)")
        btbl.add_column("Reason")
        btbl.add_column("Tools")
        btbl.add_column("Files", justify="right")
        for b in backups[:10]:
            btbl.add_row(b.backup_id, b.created_at_utc, b.reason, ", ".join(b.tools), str(len(b.entries)))
        console.print(btbl)

    ledgers = LedgerStore(cfg).list_apply_ledgers(limit=5)
    if ledgers:
        ltbl = Table(title="Recent Apply Ledgers (Per-Item SHA-256)")
        ltbl.add_column("Timestamp")
        ltbl.add_column("Mode")
        ltbl.add_column("Tools")
        ltbl.add_column("Items", justify="right")
        ltbl.add_column("Plan Hash")
        for le in ledgers:
            ltbl.add_row(le.timestamp_utc[:19], le.mode, ", ".join(le.tools), str(len(le.items)), le.plan_hash[:12])
        console.print(ltbl)

    if runs:
        rtbl = Table(title="Recent Sync Runs")
        rtbl.add_column("Mode")
        rtbl.add_column("Status")
        rtbl.add_column("Backup ID")
        rtbl.add_column("Summary")
        for r in runs:
            rtbl.add_row(r["mode"], r["status"], r["backup_id"] or "-", r["summary_json"])
        console.print(rtbl)

    if conflicts:
        ctbl = Table(title="Conflict Archive")
        ctbl.add_column("Category")
        ctbl.add_column("Entity")
        ctbl.add_column("Winner")
        ctbl.add_column("Loser")
        ctbl.add_column("Backup Path")
        for c in conflicts:
            ctbl.add_row(c["category"], c["entity_id"], c["winner_tool"], c["loser_tool"], c["backup_path"])
        console.print(ctbl)


@app.command("restore")
def cmd_restore(
    backup_id: str = typer.Argument(..., help="Backup identifier from 'agentsync status'."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Restore all backed-up tool files, SQLite databases, and sync state with SHA-256 verification."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters_by_name = {a.tool_name: a for a in _build_adapters(cfg)}
    bmgr = BackupManager(cfg)
    try:
        res = bmgr.restore_backup(backup_id, adapters_by_name=adapters_by_name, force=force)
    except (AppsRunningError, FileNotFoundError, ValueError) as exc:
        console.print(f"[bold red]Restore failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Restored backup '{res.backup_id}' successfully:[/green] "
        f"{res.restored_files} file(s) restored, {res.removed_files} newer file(s) removed."
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
