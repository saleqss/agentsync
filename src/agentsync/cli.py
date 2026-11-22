"""Typer + Rich CLI for agentsync (100% English)."""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agentsync import __version__
from agentsync.adapters.base import ToolAdapter
from agentsync.adapters.registry import get_registry
from agentsync.config import (
    ALL_ENTITIES,
    AgentSyncConfig,
    ConfigManager,
    EntityType,
    SyncOptions,
    ToolConfig,
)
from agentsync.discovery import discover_tools, merge_discovery_into_config
from agentsync.engine.bootstrap import BootstrapEngine
from agentsync.engine.doctor import DoctorEngine
from agentsync.engine.sync import SyncEngine
from agentsync.engine.watcher import SyncWatcher
from agentsync.models.canonical import SyncPlan
from agentsync.state.backup import BackupManager
from agentsync.state.state_db import StateStore
from agentsync.utils.process_guard import AppsRunningError, detect_running_apps
from agentsync.utils.secrets import mask_text

# Prevent UnicodeEncodeError on legacy Windows PowerShell 5.1 consoles
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

app = typer.Typer(
    name="agentsync",
    help="Reliable bidirectional synchronization CLI for AI coding agents.",
    no_args_is_help=True,
)
tools_app = typer.Typer(help="Manage enabled AI tools, paths, and per-tool entity filters.")
app.add_typer(tools_app, name="tools")
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


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"agentsync {__version__}")
        raise typer.Exit()


@app.callback()
def _root_callback(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show agentsync version and exit.",
    ),
) -> None:
    _ = version


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


def _build_adapters(cfg: AgentSyncConfig) -> list[ToolAdapter]:
    registry = get_registry()
    return registry.create_enabled(cfg)


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
    detail_table.add_column("Target", no_wrap=True)
    detail_table.add_column("Entity", overflow="fold", ratio=1)
    detail_table.add_column("Summary", overflow="fold", ratio=2)

    skill_bulk: dict[tuple[str, str], list[str]] = {}
    for act in plan.actions:
        if not show_all and act.category == "skills" and act.action in ("DELETE", "CREATE"):
            skill_bulk.setdefault((act.action, act.target_tool), []).append(act.entity_id)
            continue
        color = _ACTION_COLORS.get(act.action, "white")
        detail_table.add_row(
            act.category,
            f"[{color}]{act.action}[/{color}]",
            act.target_tool,
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
            f"{len(names)} skills",
            preview,
        )

    console.print(detail_table)

    if plan.warnings:
        console.print(
            Panel(
                "\n".join(f"- {mask_text(w)}" for w in plan.warnings),
                title="[yellow]Warnings[/yellow]",
                border_style="yellow",
            )
        )


# --- Discovery & Tool Management Commands ---


@app.command("discover")
def cmd_discover(
    save: bool = typer.Option(False, "--save", help="Persist discovered tools to config.toml."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
) -> None:
    """Auto-detect installed AI coding tools, storage paths, and capabilities."""
    discovered = discover_tools()
    mgr = ConfigManager(config_file)
    existing_cfg = mgr.load()

    tbl = Table(title="Auto-Discovered AI Coding Tools")
    tbl.add_column("Tool", style="bold")
    tbl.add_column("Installed")
    tbl.add_column("Config Status")
    tbl.add_column("Detected Path", overflow="fold")
    tbl.add_column("Supported Entities")

    for name, info in sorted(discovered.items()):
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

    tbl = Table(title="Registered AI Tools & Capabilities")
    tbl.add_column("Tool", style="bold")
    tbl.add_column("Enabled")
    tbl.add_column("Path", overflow="fold")
    tbl.add_column("Configured Entities")
    tbl.add_column("Adapter Capabilities")

    for name in registry.list_names():
        cls = registry.get(name)
        tcfg = cfg.tools.get(name) or ToolConfig(enabled=False, path=discovered.get(name).root_path if name in discovered else None)
        caps = [e for e in ALL_ENTITIES if cls.capabilities.supports(e)]
        if cls.capabilities.chats and not cls.capabilities.chat_write:
            caps = [f"{c}(ro)" if c == "chats" else c for c in caps]
        tbl.add_row(
            name,
            "[green]enabled[/green]" if tcfg.enabled else "[dim]disabled[/dim]",
            tcfg.path or "-",
            ", ".join(tcfg.entities),
            ", ".join(caps),
        )
    console.print(tbl)


@tools_app.command("enable")
def cmd_tools_enable(
    tool: str = typer.Argument(..., help="Tool identifier (e.g., codex, antigravity, claude-code, opencode, deepseek-harness)."),
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
    entities: str = typer.Argument(..., help="Comma-separated entity list (projects,instructions,skills,mcp,plugins,chats)."),
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


# --- Operational Commands ---


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
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview repairs without modifying disk (used with --fix)."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard when fixing."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Validate configs, SQLite stores, protobuf indexes, and session JSONL schemas across enabled tools."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    doc_engine = DoctorEngine(cfg, adapters)

    issues = doc_engine.diagnose()
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
        try:
            results, backup_id = doc_engine.repair(dry_run=dry_run, force=force)
        except AppsRunningError as exc:
            console.print(f"[bold red]Error:[/bold red] {exc}")
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
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Quarantine and regenerate broken rollouts and out-of-sync summary indexes."""
    cmd_doctor(fix=True, dry_run=dry_run, force=force, config_file=config_file, data_dir=data_dir)


@app.command("bootstrap")
def cmd_bootstrap(
    source_tool: str = typer.Option(..., "--from", help="Authoritative source tool for initial skills/MCP/plugins migration."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show diff plan without modifying any files."),
    force: bool = typer.Option(False, "--force", help="Ignore running application lock guard."),
    include_subagents: bool = typer.Option(False, "--include-subagents", help="Include subagent chats."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every individual skill entry."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Initial clean migration: backup -> clear target skills/MCP/plugins -> import from source -> merge projects/rules/chats."""
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
    backup: bool = typer.Option(False, "--backup", help="Create a versioned backup before applying changes."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every individual skill entry."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Idempotent bidirectional synchronization across all enabled AI tools."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    options = SyncOptions(dry_run=dry_run, force=force, include_subagents=include_subagents)
    engine = SyncEngine(cfg, adapters, options=options)

    try:
        plan = engine.run(dry_run=dry_run, force=force, create_backup=backup)
    except AppsRunningError as exc:
        console.print(f"[bold red]Error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_plan(plan, show_all=verbose)


@app.command("watch")
def cmd_watch(
    debounce: float = typer.Option(2.0, "--debounce", help="Debounce quiet interval in seconds."),
    force: bool = typer.Option(False, "--force", help="Allow writes while target apps are running."),
    include_subagents: bool = typer.Option(False, "--include-subagents", help="Include subagent chats."),
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Run continuous debounced file-watching synchronization in the background."""
    _, cfg = _load_effective_config(config_file, data_dir)
    adapters = _build_adapters(cfg)
    options = SyncOptions(
        dry_run=False,
        force=force,
        include_subagents=include_subagents,
        debounce_seconds=debounce,
    )

    def _on_done(plan: SyncPlan) -> None:
        if plan.is_empty:
            console.print("[dim]watch: no changes detected[/dim]")
        else:
            console.print(
                f"[green]watch: applied {len(plan.mutating_actions)} action(s) "
                f"(conflicts={len(plan.conflict_actions)}, skipped={len(plan.skipped_actions)})[/green]"
            )

    def _on_err(exc: Exception) -> None:
        console.print(f"[yellow]watch: {mask_text(str(exc))}[/yellow]")

    console.print(
        f"[cyan]Starting watch mode across {', '.join(a.tool_name for a in adapters)} "
        f"(debounce={debounce}s). Press Ctrl+C to stop.[/cyan]"
    )
    watcher = SyncWatcher(cfg, adapters, options=options, on_sync_done=_on_done, on_error=_on_err)
    watcher.run_forever()


@app.command("status")
def cmd_status(
    config_file: Path | None = typer.Option(None, "--config", help="Override config.toml path."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Override agentsync data directory."),
) -> None:
    """Show synced entity counts, available backups, recent runs, and conflict history."""
    _, cfg = _load_effective_config(config_file, data_dir)
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
