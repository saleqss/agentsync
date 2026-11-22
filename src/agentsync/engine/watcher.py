"""Debounced filesystem watcher (`watch` mode) for continuous background synchronization."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from agentsync.adapters.base import ToolAdapter
from agentsync.config import AgentSyncConfig, SyncOptions
from agentsync.engine.sync import SyncEngine
from agentsync.models.canonical import SyncPlan
from agentsync.utils.process_guard import AppsRunningError

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


def _is_relevant_path(path_str: str) -> bool:
    low = path_str.lower().replace("\\", "/")
    if any(low.endswith(suf) for suf in _IGNORED_SUFFIXES) or ".tmp." in low:
        return False
    parts = [p for p in low.split("/") if p]
    return not any(p in _IGNORED_PATH_PARTS for p in parts)


class _DebouncedSyncHandler(FileSystemEventHandler):
    def __init__(self, debounce_seconds: float, on_trigger: Callable[[], None]) -> None:
        self.debounce_seconds = debounce_seconds
        self.on_trigger = on_trigger
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._suppress_until: float = 0.0

    def suppress_for(self, seconds: float) -> None:
        with self._lock:
            self._suppress_until = max(self._suppress_until, time.monotonic() + seconds)

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or not _is_relevant_path(str(event.src_path)):
            return
        with self._lock:
            if time.monotonic() < self._suppress_until:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_seconds, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            if time.monotonic() < self._suppress_until:
                return
        self.on_trigger()

    def stop(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class SyncWatcher:
    """Monitors all enabled tool directories and triggers debounced bidirectional syncs."""

    def __init__(
        self,
        config: AgentSyncConfig,
        adapters: list[ToolAdapter],
        options: SyncOptions,
        on_sync_done: Callable[[SyncPlan], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.config = config
        self.adapters = adapters
        self.options = options
        self.on_sync_done = on_sync_done
        self.on_error = on_error
        self._engine = SyncEngine(config, adapters, options=options)
        self._sync_lock = threading.Lock()
        self._handler = _DebouncedSyncHandler(
            debounce_seconds=options.debounce_seconds,
            on_trigger=self._execute_sync,
        )
        self._observer = Observer()

    def _execute_sync(self) -> None:
        if not self._sync_lock.acquire(blocking=False):
            return
        try:
            self._handler.suppress_for(self.options.debounce_seconds + 1.5)
            plan = self._engine.run(dry_run=False, force=self.options.force)
            self._handler.suppress_for(1.0)
            if self.on_sync_done:
                self.on_sync_done(plan)
        except AppsRunningError as exc:
            if self.on_error:
                self.on_error(exc)
        except Exception as exc:
            if self.on_error:
                self.on_error(exc)
        finally:
            self._sync_lock.release()

    def run_forever(self) -> None:
        seen: set[str] = set()
        for adapter in self.adapters:
            for root in adapter.watch_roots():
                key = str(root.resolve())
                if key not in seen and root.exists():
                    seen.add(key)
                    self._observer.schedule(self._handler, str(root), recursive=True)
        self._observer.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._handler.stop()
            self._observer.stop()
            self._observer.join(timeout=5.0)
