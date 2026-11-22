"""Debounced filesystem watcher (`watch` / daemon mode) with Sync-on-Close deferred queue."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import BaseModel, Field
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig, SyncOptions
from agentsync.engine.sync import SyncEngine
from agentsync.models.canonical import SyncPlan
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
}
_IGNORED_SUFFIXES = (".tmp", ".lock", "-wal", "-shm", "-journal", ".bak")


class WatcherStatusSnapshot(BaseModel):
    running: bool = False
    paused: bool = False
    watched_roots: list[str] = Field(default_factory=list)
    running_locked_apps: list[str] = Field(default_factory=list)
    pending_queue: list[str] = Field(default_factory=list)
    sync_count: int = 0
    last_sync_utc: str | None = None
    last_error: str | None = None
    daemon_log_path: str = ""


def _is_relevant_path(path_str: str) -> bool:
    low = path_str.lower().replace("\\", "/")
    if any(low.endswith(suf) for suf in _IGNORED_SUFFIXES) or ".tmp." in low:
        return False
    parts = [p for p in low.split("/") if p]
    return not any(p in _IGNORED_PATH_PARTS for p in parts)


def _create_daemon_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(f"agentsync.daemon.{id(log_path)}")
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
    """Monitors enabled tool directories with automatic Sync-on-Close deferred queuing and backups."""

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
    ) -> None:
        self.config = config
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
        self._sync_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._paused = False
        self._running = False

        self._watched_roots: list[str] = []
        self._pending_queue: list[str] = []
        self._running_locked_apps: list[str] = []
        self._last_locked_apps: set[str] = set()
        self._apps_closed_at: float | None = None
        self._sync_count = 0
        self._last_sync_utc: str | None = None
        self._last_error: str | None = None

        self._logger = _create_daemon_logger(config.daemon_log_path) if daemon_mode else None
        self._handler = _DebouncedSyncHandler(
            debounce_seconds=options.debounce_seconds,
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

    def _check_locked_apps(self) -> list[str]:
        if self.options.force:
            return []
        procs = self._process_detector([a.tool_name for a in self.adapters])
        return sorted({p.app for p in procs})

    def enqueue_change(self, label: str) -> None:
        """Manually queue a pending change (useful for startup deferral or tests)."""
        with self._state_lock:
            if label not in self._pending_queue:
                self._pending_queue.append(label)

    def _handle_change_batch(self, changed_paths: list[str]) -> None:
        with self._state_lock:
            if self._paused:
                for p in changed_paths:
                    if p not in self._pending_queue:
                        self._pending_queue.append(p)
                self._emit_log("info", f"Paused: queued {len(changed_paths)} change(s).")
                return

        locked = self._check_locked_apps()
        with self._state_lock:
            self._running_locked_apps = locked
        if locked:
            with self._state_lock:
                for p in (changed_paths or ["pending-sync"]):
                    if p not in self._pending_queue:
                        self._pending_queue.append(p)
                self._last_locked_apps = set(locked)
                self._apps_closed_at = None
            self._emit_log(
                "warning",
                f"Sync-on-Close deferred: {', '.join(locked)} running "
                f"(queued {len(self._pending_queue)} pending item(s)).",
            )
            return

        self._execute_sync(reason="watch-change")

    def _execute_sync(self, *, reason: str = "watch") -> SyncPlan | None:
        if not self._sync_lock.acquire(blocking=False):
            return None
        try:
            self._handler.suppress_for(self.options.debounce_seconds + 1.5)
            # Note: watcher checks process locks via injectable `_process_detector`, then invokes SyncEngine
            locked = self._check_locked_apps()
            if locked:
                with self._state_lock:
                    self._running_locked_apps = locked
                    if reason not in self._pending_queue:
                        self._pending_queue.append(reason)
                    self._last_locked_apps = set(locked)
                    self._apps_closed_at = None
                self._emit_log(
                    "warning",
                    f"Deferred sync ({reason}): {', '.join(locked)} became active.",
                )
                return None

            plan = self._engine.run(
                dry_run=False,
                force=True,  # already verified via _check_locked_apps() above
                check_processes=False,
                create_backup=True,
            )
            self._handler.suppress_for(1.0)
            with self._state_lock:
                self._pending_queue.clear()
                self._sync_count += 1
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

    def _poll_sync_on_close_once(self) -> bool:
        """Check whether previously locked apps have exited and flush deferred queue after grace delay."""
        locked = self._check_locked_apps()
        should_flush = False
        now = time.monotonic()

        with self._state_lock:
            self._running_locked_apps = locked
            if self._paused:
                return False
            if locked:
                self._last_locked_apps = set(locked)
                self._apps_closed_at = None
                return False

            # Apps are now closed! Did a tracked app just exit OR do we have queued events waiting?
            had_running = bool(self._last_locked_apps)
            has_pending = bool(self._pending_queue)
            if had_running and not has_pending:
                # An app that was running just closed -> schedule a Sync-on-Close pass
                self._pending_queue.append(f"app-closed:{','.join(sorted(self._last_locked_apps))}")
                has_pending = True

            if has_pending:
                if self._apps_closed_at is None:
                    self._apps_closed_at = now
                    self._last_locked_apps.clear()
                    self._emit_log(
                        "info",
                        f"Apps closed. Waiting {self.close_grace_seconds:.1f}s for WAL flush before Sync-on-Close...",
                    )
                elif now - self._apps_closed_at >= self.close_grace_seconds:
                    self._apps_closed_at = None
                    should_flush = True

        if should_flush:
            self._execute_sync(reason="sync-on-close")
            return True
        return False

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.poll_interval_seconds):
            try:
                self._poll_sync_on_close_once()
            except Exception as exc:
                self._emit_log("error", f"Process monitor error: {exc}")

    def start(self) -> None:
        """Start filesystem observer and Sync-on-Close monitor without blocking caller thread."""
        with self._state_lock:
            if self._running:
                return
            self._stop_event.clear()
            self._paused = False
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
            f"{', '.join(a.tool_name for a in self.adapters)}.",
        )

        if self.run_startup_sync:
            if self._running_locked_apps:
                self.enqueue_change("startup-sync")
                self._emit_log(
                    "warning",
                    f"Startup sync queued for Sync-on-Close ({', '.join(self._running_locked_apps)} running).",
                )
            else:
                self._execute_sync(reason="startup")

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="agentsync-sync-on-close",
            daemon=True,
        )
        self._monitor_thread.start()

    def pause(self) -> None:
        with self._state_lock:
            self._paused = True
        self._emit_log("info", "Watcher paused (events will queue until resumed).")

    def resume(self) -> None:
        with self._state_lock:
            self._paused = False
            has_pending = bool(self._pending_queue)
        self._emit_log("info", "Watcher resumed.")
        if has_pending:
            self._handle_change_batch([])

    def stop(self) -> None:
        self._stop_event.set()
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
        with self._state_lock:
            self._running = False
            self._paused = False
        self._emit_log("info", "Watcher stopped.")

    def status_snapshot(self) -> WatcherStatusSnapshot:
        with self._state_lock:
            return WatcherStatusSnapshot(
                running=self._running,
                paused=self._paused,
                watched_roots=list(self._watched_roots),
                running_locked_apps=list(self._running_locked_apps),
                pending_queue=list(self._pending_queue),
                sync_count=self._sync_count,
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
