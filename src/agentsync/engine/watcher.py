"""Split-tier real-time filesystem watcher with incremental dirty-file cache and zero idle polling."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig, SyncOptions
from agentsync.engine.diagnostics import DiagnosticsEngine, load_repair_queue
from agentsync.engine.sync import SyncEngine
from agentsync.models.canonical import SyncPlan
from agentsync.utils.atomic_io import atomic_write_json, sha256_file
from agentsync.utils.notify import send_desktop_notification
from agentsync.utils.process_guard import AppsRunningError, RunningAppProcess, detect_running_apps

_IGNORED_PATH_PARTS = {
    ".tmp",
    "cache",
    "code cache",
    "gpucache",
    "logs",
    "blob_storage",
    "network",
    "crashes",
    ".git",
    "__pycache__",
    "node_modules",
    "thread-writer-locks",
    "project-metadata-locks",
    "mcp-oauth-locks",
    "backups",
    "quarantine",
    "ledgers",
    "conflicts",
    "db-backups",
}
_IGNORED_SUFFIXES = (".tmp", ".lock", "-wal", "-shm", "-journal", ".bak", ".log")

# Tier 2 (lock-sensitive SQLite / WAL / protobuf / rollout session stores)
_TIER2_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".pb", ".vscdb", ".jsonl", "-wal", "-shm", "-journal")
_TIER2_PATH_PARTS = {"sessions", "conversations", "sqlite", "globalstorage", "brain", "annotations"}

# Tier 1 unlocked categories synced in real time even while desktop apps are open
TIER1_INCLUDE_SELECTORS: list[str] = ["skills", "instructions", "mcp", "plugins"]


class WatcherStatusSnapshot(BaseModel):
    running: bool = False
    paused: bool = False
    battery_paused: bool = False
    watched_roots: list[str] = Field(default_factory=list)
    running_locked_apps: list[str] = Field(default_factory=list)
    pending_tier1_queue: list[str] = Field(default_factory=list)
    pending_queue: list[str] = Field(default_factory=list)
    pending_repair_count: int = 0
    sync_count: int = 0
    tier1_sync_count: int = 0
    tier2_sync_count: int = 0
    cache_hits_skipped: int = 0
    last_sync_utc: str | None = None
    last_error: str | None = None
    daemon_log_path: str = ""


def _is_relevant_path(path_str: str) -> bool:
    low = path_str.lower().replace("\\", "/")
    if any(low.endswith(suf) for suf in _IGNORED_SUFFIXES) or ".tmp." in low:
        return False
    parts = [p for p in low.split("/") if p]
    return not any(p in _IGNORED_PATH_PARTS for p in parts)


def classify_change_tier(path_str: str | Path) -> Literal["tier1", "tier2"]:
    """Classify a changed file as Tier 1 (unlocked instant sync) or Tier 2 (locked Sync-on-Close)."""
    low = str(path_str).lower().replace("\\", "/")
    name = low.rsplit("/", 1)[-1]
    if name == ".codex-global-state.json" or name == "session_index.jsonl":
        return "tier2"
    if any(low.endswith(suf) for suf in _TIER2_SUFFIXES):
        return "tier2"
    parts = set(p for p in low.split("/") if p)
    if parts & _TIER2_PATH_PARTS:
        return "tier2"
    return "tier1"


class DirtyFileCache:
    """Tracks (path -> mtime_ns, size, sha256) to skip unchanged files and suppress self-write loops."""

    def __init__(self, cache_path: Path | None = None) -> None:
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[int, int, str]] = {}
        self._self_written_hashes: dict[str, tuple[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, list) and len(v) == 3:
                        self._entries[k] = (int(v[0]), int(v[1]), str(v[2]))
        except Exception:
            pass

    def save(self) -> None:
        if not self.cache_path:
            return
        with self._lock:
            payload = {k: [v[0], v[1], v[2]] for k, v in list(self._entries.items())[-4000:]}
        try:
            atomic_write_json(self.cache_path, payload)
        except Exception:
            pass

    def record_self_write(self, path: Path, ttl_seconds: float = 5.0) -> None:
        """Mark a file's current hash as written by agentsync itself to suppress echo loops."""
        if not path.exists() or not path.is_file():
            return
        try:
            digest = sha256_file(path)
            key = os.path.normcase(os.path.abspath(str(path)))
            with self._lock:
                self._self_written_hashes[key] = (digest, time.monotonic() + ttl_seconds)
        except OSError:
            pass

    def is_dirty(self, path_str: str) -> bool:
        """Return True only if file actually changed content and was not just written by agentsync."""
        p = Path(path_str)
        key = os.path.normcase(os.path.abspath(path_str))
        if not p.exists():
            with self._lock:
                existed = key in self._entries
                self._entries.pop(key, None)
            return existed
        if not p.is_file():
            return False
        try:
            st = p.stat()
        except OSError:
            return False

        with self._lock:
            prev = self._entries.get(key)
            if prev is not None and prev[0] == st.st_mtime_ns and prev[1] == st.st_size:
                return False

        try:
            digest = sha256_file(p)
        except OSError:
            return True

        now = time.monotonic()
        with self._lock:
            # Suppress feedback loop if this exact digest was written by agentsync seconds ago
            sw = self._self_written_hashes.get(key)
            if sw is not None:
                if now <= sw[1] and sw[0] == digest:
                    self._entries[key] = (st.st_mtime_ns, st.st_size, digest)
                    return False
                if now > sw[1]:
                    self._self_written_hashes.pop(key, None)

            prev = self._entries.get(key)
            self._entries[key] = (st.st_mtime_ns, st.st_size, digest)
            if prev is not None and prev[2] == digest:
                return False
        return True


def _create_daemon_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"agentsync.daemon.{os.path.normcase(str(log_path))}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    return logger


class _DebouncedSyncHandler(FileSystemEventHandler):
    def __init__(
        self,
        debounce_seconds: float,
        on_trigger: Callable[[list[str]], None],
    ) -> None:
        self.debounce_seconds = debounce_seconds
        self.on_trigger = on_trigger
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._suppress_until: float = 0.0
        self._buffered_paths: list[str] = []

    def set_debounce(self, seconds: float) -> None:
        with self._lock:
            self.debounce_seconds = max(0.2, seconds)

    def suppress_for(self, seconds: float) -> None:
        with self._lock:
            self._suppress_until = max(self._suppress_until, time.monotonic() + seconds)

    def on_any_event(self, event: FileSystemEvent) -> None:
        src = str(event.src_path)
        if event.is_directory or not _is_relevant_path(src):
            return
        with self._lock:
            if time.monotonic() < self._suppress_until:
                return
            if src not in self._buffered_paths:
                self._buffered_paths.append(src)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            if time.monotonic() < self._suppress_until:
                self._buffered_paths.clear()
                return
            paths = list(self._buffered_paths)
            self._buffered_paths.clear()
        self.on_trigger(paths)

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._buffered_paths.clear()


class SyncWatcher:
    """Split-tier event-driven watcher: Tier-1 instant unlocked sync + Tier-2 Sync-on-Close & Auto-Heal."""

    def __init__(
        self,
        config: AgentSyncConfig,
        adapters: list[ToolAdapter],
        options: SyncOptions,
        on_sync_done: Callable[[SyncPlan], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        *,
        daemon_mode: bool = False,
        run_startup_sync: bool = True,
        poll_interval_seconds: float = 1.0,
        close_grace_seconds: float | None = None,
        process_detector: Callable[[Iterable[str] | None], list[RunningAppProcess]] | None = None,
        config_path: Path | None = None,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.adapters = adapters
        self.options = options
        self.on_sync_done = on_sync_done
        self.on_error = on_error
        self.on_log = on_log
        self.daemon_mode = daemon_mode
        self.run_startup_sync = run_startup_sync
        self.poll_interval_seconds = max(0.02, poll_interval_seconds)
        self.close_grace_seconds = (
            close_grace_seconds if close_grace_seconds is not None else config.close_grace_seconds
        )
        self._process_detector = process_detector or (
            lambda check_apps=None: detect_running_apps(check_apps=check_apps)
        )

        self._engine = SyncEngine(config, adapters, options=options)
        self._tier1_engine = SyncEngine(
            config,
            adapters,
            options=options.model_copy(update={"include": list(TIER1_INCLUDE_SELECTORS)}),
        )
        self._file_cache = DirtyFileCache(config.file_cache_path if daemon_mode else None)
        self._sync_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._wake_cond = threading.Condition(self._state_lock)
        self._stop_event = threading.Event()
        self._paused = not config.realtime_sync_enabled
        self._battery_paused = False
        self._running = False

        self._watched_roots: list[str] = []
        self._pending_tier1_queue: list[str] = []
        self._pending_queue: list[str] = []  # Tier 2 locked DB queue
        self._running_locked_apps: list[str] = []
        self._last_locked_apps: set[str] = set()
        self._apps_closed_at: float | None = None
        self._sync_count = 0
        self._tier1_sync_count = 0
        self._tier2_sync_count = 0
        self._cache_hits_skipped = 0
        self._last_sync_utc: str | None = None
        self._last_error: str | None = None

        self._logger = _create_daemon_logger(config.daemon_log_path) if daemon_mode else None
        self._handler = _DebouncedSyncHandler(
            debounce_seconds=options.debounce_seconds or config.effective_debounce_seconds(),
            on_trigger=self._handle_change_batch,
        )
        self._observer: Observer | None = None
        self._monitor_thread: threading.Thread | None = None

    def _emit_log(self, level: str, message: str) -> None:
        if self._logger is not None:
            fn = getattr(self._logger, level.lower(), self._logger.info)
            fn(message)
        if self.on_log is not None:
            try:
                self.on_log(level, message)
            except Exception:
                pass

    def _wake_monitor(self) -> None:
        with self._wake_cond:
            self._wake_cond.notify_all()

    def _check_locked_apps(self) -> list[str]:
        if self.options.force:
            return []
        procs = self._process_detector([a.tool_name for a in self.adapters])
        return sorted({p.app for p in procs})

    def _has_queued_repairs(self) -> bool:
        return not load_repair_queue(self.config.repair_queue_path).is_empty

    def reload_config(self, new_config: AgentSyncConfig, new_adapters: list[ToolAdapter]) -> None:
        """Hot-reload configuration and adapters live without restarting the daemon."""
        with self._state_lock:
            self.config = new_config
            self.adapters = new_adapters
            self.close_grace_seconds = new_config.close_grace_seconds
            self._paused = not new_config.realtime_sync_enabled
            self.options = self.options.model_copy(
                update={
                    "debounce_seconds": new_config.effective_debounce_seconds(),
                    "strip_secrets": new_config.strip_secrets,
                    "strict_vocab_guard": new_config.strict_vocab_guard,
                    "skill_link_mode": new_config.skill_link_mode,
                    "include_subagents": new_config.include_subagents,
                }
            )
            self._engine = SyncEngine(new_config, new_adapters, options=self.options)
            self._tier1_engine = SyncEngine(
                new_config,
                new_adapters,
                options=self.options.model_copy(update={"include": list(TIER1_INCLUDE_SELECTORS)}),
            )
        self._handler.set_debounce(self.options.debounce_seconds)
        self._emit_log(
            "info",
            f"Hot-reloaded config (profile={new_config.power_profile}, debounce={self.options.debounce_seconds}s, "
            f"realtime={new_config.realtime_sync_enabled}, auto_heal={new_config.auto_heal_on_close}).",
        )
        self._wake_monitor()

    def set_battery_paused(self, paused: bool) -> None:
        with self._state_lock:
            if self._battery_paused == paused:
                return
            self._battery_paused = paused
        self._emit_log(
            "info",
            "Battery-Safe profile: paused background sync on battery."
            if paused
            else "Battery-Safe profile: resumed background sync on AC power.",
        )
        if not paused:
            self._wake_monitor()

    def enqueue_change(self, label: str) -> None:
        """Queue a pending Tier-2 Sync-on-Close change and wake the monitor thread."""
        with self._state_lock:
            if label not in self._pending_queue:
                self._pending_queue.append(label)
        self._wake_monitor()

    def notify_repair_queued(self) -> None:
        """Wake the idle monitor thread because new repairs were added to repair_queue.json."""
        self._wake_monitor()

    def _is_effectively_paused(self) -> bool:
        return self._paused or self._battery_paused

    def _handle_change_batch(self, changed_paths: list[str]) -> None:
        # Filter unchanged / self-written paths through incremental dirty-file cache
        dirty_paths: list[str] = []
        if not changed_paths:
            dirty_paths = []
        else:
            for p in changed_paths:
                if Path(p).exists() and not self._file_cache.is_dirty(p):
                    with self._state_lock:
                        self._cache_hits_skipped += 1
                    continue
                dirty_paths.append(p)
            if not dirty_paths:
                return

        with self._state_lock:
            if self._is_effectively_paused():
                for p in dirty_paths:
                    if classify_change_tier(p) == "tier1":
                        if p not in self._pending_tier1_queue:
                            self._pending_tier1_queue.append(p)
                    else:
                        if p not in self._pending_queue:
                            self._pending_queue.append(p)
                self._emit_log("info", f"Paused: queued {len(dirty_paths)} dirty change(s).")
                return

        tier1_paths = [p for p in dirty_paths if classify_change_tier(p) == "tier1"]
        tier2_paths = [p for p in dirty_paths if classify_change_tier(p) == "tier2"]

        # Tier 1: Unlocked skills / instructions / plugins / MCP -> Instant real-time sync!
        if tier1_paths:
            self._execute_tier1_sync(reason="realtime-tier1")

        # Tier 2: Locked SQLite / protobuf / sessions -> Check locks; defer if running!
        if tier2_paths or not dirty_paths:
            locked = self._check_locked_apps()
            with self._state_lock:
                self._running_locked_apps = locked
            if locked:
                if self.config.sync_on_close_enabled:
                    with self._state_lock:
                        for p in (tier2_paths or ["pending-sync"]):
                            if p not in self._pending_queue:
                                self._pending_queue.append(p)
                        self._last_locked_apps = set(locked)
                        self._apps_closed_at = None
                    self._emit_log(
                        "warning",
                        f"Tier-2 Sync-on-Close deferred: {', '.join(locked)} running "
                        f"({len(self._pending_queue)} queued).",
                    )
                    self._wake_monitor()
                return
            self._execute_full_sync(reason="watch-tier2")

    def _execute_tier1_sync(self, *, reason: str = "realtime-tier1") -> SyncPlan | None:
        """Fast unlocked sync for skills, instructions, plugins, and portable MCP configs."""
        if not self._sync_lock.acquire(blocking=False):
            return None
        try:
            self._handler.suppress_for(self.options.debounce_seconds + 1.0)
            plan = self._tier1_engine.run(
                dry_run=False,
                force=True,  # Tier 1 never touches locked SQLite/protobuf session DBs
                check_processes=False,
                create_backup=self.config.auto_backup,
            )
            self._handler.suppress_for(0.8)
            with self._state_lock:
                self._pending_tier1_queue.clear()
                self._sync_count += 1
                self._tier1_sync_count += 1
                self._last_sync_utc = datetime.now(UTC).isoformat()
                self._last_error = None
            mut = len(plan.mutating_actions)
            if mut > 0:
                self._emit_log(
                    "info",
                    f"Tier-1 instant sync ({reason}): applied {mut} change(s) [backup={plan.backup_id or 'none'}].",
                )
                send_desktop_notification(
                    "AgentSync Real-Time Sync",
                    f"Synced {mut} skill/rule/MCP change(s).",
                    enabled=self.config.notifications,
                )
            if self.on_sync_done:
                self.on_sync_done(plan)
            return plan
        except Exception as exc:
            with self._state_lock:
                self._last_error = str(exc)
            self._emit_log("error", f"Tier-1 sync error: {exc}")
            if self.on_error:
                self.on_error(exc)
            return None
        finally:
            self._sync_lock.release()

    def _execute_full_sync(self, *, reason: str = "watch") -> SyncPlan | None:
        """Full bidirectional sync across all entities (strictly guarded against locked DB apps)."""
        if not self._sync_lock.acquire(blocking=False):
            return None
        try:
            self._handler.suppress_for(self.options.debounce_seconds + 1.5)
            locked = self._check_locked_apps()
            if locked:
                with self._state_lock:
                    self._running_locked_apps = locked
                    if reason not in self._pending_queue:
                        self._pending_queue.append(reason)
                    self._last_locked_apps = set(locked)
                    self._apps_closed_at = None
                self._emit_log("warning", f"Deferred sync ({reason}): {', '.join(locked)} active.")
                self._wake_monitor()
                return None

            plan = self._engine.run(
                dry_run=False,
                force=True,  # already verified via _check_locked_apps() above
                check_processes=False,
                create_backup=self.config.auto_backup,
            )
            self._handler.suppress_for(1.0)
            with self._state_lock:
                self._pending_queue.clear()
                self._pending_tier1_queue.clear()
                self._sync_count += 1
                self._tier2_sync_count += 1
                self._last_sync_utc = datetime.now(UTC).isoformat()
                self._last_error = None
            mut = len(plan.mutating_actions)
            if mut > 0:
                self._emit_log(
                    "info",
                    f"Sync ({reason}) applied {mut} change(s) [backup={plan.backup_id or 'none'}].",
                )
                send_desktop_notification(
                    "AgentSync Completed",
                    f"Synced {mut} change(s) across {len(self.adapters)} tool(s).",
                    enabled=self.config.notifications,
                )
            else:
                self._emit_log("info", f"Sync ({reason}) verified 0 diff (already in sync).")
            if self.on_sync_done:
                self.on_sync_done(plan)
            return plan
        except AppsRunningError as exc:
            with self._state_lock:
                self._last_error = str(exc)
                if reason not in self._pending_queue:
                    self._pending_queue.append(reason)
            self._emit_log("warning", f"Deferred due to running apps: {exc}")
            if self.on_error:
                self.on_error(exc)
            self._wake_monitor()
            return None
        except Exception as exc:
            with self._state_lock:
                self._last_error = str(exc)
            self._emit_log("error", f"Watch sync failed: {exc}")
            if self.on_error:
                self.on_error(exc)
            return None
        finally:
            self._sync_lock.release()

    def _execute_queued_repairs_if_ready(self) -> bool:
        if not self._has_queued_repairs():
            return False
        if not self._sync_lock.acquire(blocking=False):
            return False
        try:
            locked = self._check_locked_apps()
            if locked:
                with self._state_lock:
                    self._running_locked_apps = locked
                    self._last_locked_apps = set(locked)
                    self._apps_closed_at = None
                return False
            self._handler.suppress_for(self.options.debounce_seconds + 2.0)
            diag = DiagnosticsEngine(self.config, self.adapters, config_path=self.config_path)
            res = diag.execute_queued_repairs(
                dry_run=False,
                force=True,  # already verified via _check_locked_apps()
                check_processes=False,
                safe_only=False,
            )
            if res.executed:
                self._emit_log(
                    "info",
                    f"Auto-Repair on Close fixed {res.fixed_count} issue(s) "
                    f"[backup={res.backup_id or 'none'}, remaining={res.remaining_issues}].",
                )
            return res.executed
        except Exception as exc:
            with self._state_lock:
                self._last_error = str(exc)
            self._emit_log("error", f"Auto-Repair on Close failed: {exc}")
            return False
        finally:
            self._sync_lock.release()

    def _needs_process_monitoring(self) -> bool:
        """Return True ONLY when pending Tier-2 syncs, queued repairs, or tracked locked apps exist."""
        return bool(self._pending_queue or self._last_locked_apps or self._has_queued_repairs())

    def _poll_sync_on_close_once(self) -> bool:
        """Check lock state ONLY when pending work exists; flush repairs & sync after close_grace_seconds."""
        with self._state_lock:
            if self._is_effectively_paused():
                return False
            if not self._needs_process_monitoring():
                self._running_locked_apps = []
                return False

        locked = self._check_locked_apps()
        should_flush_sync = False
        should_flush_repair = False
        now = time.monotonic()
        has_repairs = self._has_queued_repairs()

        with self._state_lock:
            self._running_locked_apps = locked
            if self._is_effectively_paused():
                return False
            if locked:
                self._last_locked_apps = set(locked)
                self._apps_closed_at = None
                return False

            had_running = bool(self._last_locked_apps)
            has_pending = bool(self._pending_queue)
            if had_running and not has_pending and self.config.sync_on_close_enabled:
                self._pending_queue.append(f"app-closed:{','.join(sorted(self._last_locked_apps))}")
                has_pending = True

            if has_pending or has_repairs:
                if self._apps_closed_at is None:
                    self._apps_closed_at = now
                    self._last_locked_apps.clear()
                    self._emit_log(
                        "info",
                        f"Apps closed. Waiting {self.close_grace_seconds:.1f}s for WAL flush before Sync/Repair-on-Close...",
                    )
                elif now - self._apps_closed_at >= self.close_grace_seconds:
                    self._apps_closed_at = None
                    should_flush_repair = has_repairs
                    should_flush_sync = has_pending

        did_work = False
        if should_flush_repair:
            did_work = self._execute_queued_repairs_if_ready() or did_work
        if should_flush_sync:
            self._execute_full_sync(reason="sync-on-close")
            did_work = True
        return did_work

    def _monitor_loop(self) -> None:
        """Event-driven monitor loop: sleeps deeply when idle (ZERO busy process polling!)."""
        while not self._stop_event.is_set():
            with self._wake_cond:
                while (
                    not self._stop_event.is_set()
                    and (self._is_effectively_paused() or not self._needs_process_monitoring())
                ):
                    self._wake_cond.wait(timeout=5.0)
            if self._stop_event.is_set():
                break
            try:
                self._poll_sync_on_close_once()
            except Exception as exc:
                self._emit_log("error", f"Process monitor error: {exc}")
            self._stop_event.wait(self.poll_interval_seconds)

    def start(self) -> None:
        """Start native OS filesystem observer and zero-idle-poll Sync-on-Close monitor."""
        with self._state_lock:
            if self._running:
                return
            self._stop_event.clear()
            self._paused = not self.config.realtime_sync_enabled
            self._watched_roots = []

        self._observer = Observer()
        seen: set[str] = set()
        for adapter in self.adapters:
            for root in adapter.watch_roots():
                if not root.exists():
                    continue
                key = str(root.resolve())
                if key not in seen:
                    seen.add(key)
                    self._watched_roots.append(key)
                    self._observer.schedule(self._handler, key, recursive=True)

        self._observer.start()
        with self._state_lock:
            self._running = True
            self._running_locked_apps = self._check_locked_apps()
            self._last_locked_apps = set(self._running_locked_apps)

        self._emit_log(
            "info",
            f"Watcher started on {len(self._watched_roots)} root(s) for "
            f"{', '.join(a.tool_name for a in self.adapters)} (profile={self.config.power_profile}).",
        )

        if self.run_startup_sync and not self._is_effectively_paused():
            if self._running_locked_apps:
                self.enqueue_change("startup-sync")
                self._emit_log(
                    "warning",
                    f"Startup sync queued for Sync-on-Close ({', '.join(self._running_locked_apps)} running).",
                )
            else:
                self._execute_full_sync(reason="startup")

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="agentsync-sync-on-close",
            daemon=True,
        )
        self._monitor_thread.start()
        self._wake_monitor()

    def pause(self) -> None:
        with self._state_lock:
            self._paused = True
        self._emit_log("info", "Watcher paused (events will queue until resumed).")

    def resume(self) -> None:
        with self._state_lock:
            self._paused = False
            has_t1 = bool(self._pending_tier1_queue)
            has_t2 = bool(self._pending_queue)
        self._emit_log("info", "Watcher resumed.")
        if has_t1:
            self._execute_tier1_sync(reason="resume-tier1")
        if has_t2:
            self._wake_monitor()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_monitor()
        self._handler.stop()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=5.0)
            except Exception:
                pass
            self._observer = None
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=3.0)
        self._monitor_thread = None
        self._file_cache.save()
        with self._state_lock:
            self._running = False
        self._emit_log("info", "Watcher stopped.")

    def status_snapshot(self) -> WatcherStatusSnapshot:
        pending_rep = len(load_repair_queue(self.config.repair_queue_path).entries)
        with self._state_lock:
            return WatcherStatusSnapshot(
                running=self._running,
                paused=self._paused,
                battery_paused=self._battery_paused,
                watched_roots=list(self._watched_roots),
                running_locked_apps=list(self._running_locked_apps),
                pending_tier1_queue=list(self._pending_tier1_queue),
                pending_queue=list(self._pending_queue),
                pending_repair_count=pending_rep,
                sync_count=self._sync_count,
                tier1_sync_count=self._tier1_sync_count,
                tier2_sync_count=self._tier2_sync_count,
                cache_hits_skipped=self._cache_hits_skipped,
                last_sync_utc=self._last_sync_utc,
                last_error=self._last_error,
                daemon_log_path=str(self.config.daemon_log_path),
            )

    def run_forever(self) -> None:
        """Blocking foreground/daemon loop until interrupted."""
        self.start()
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
