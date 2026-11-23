"""Native windowless single-instance AgentSync daemon (`agentsyncw`) with live telemetry & IPC."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psutil
from pydantic import BaseModel, Field

from agentsync import __version__
from agentsync.accel import runtime_engine_label
from agentsync.adapters.registry import get_registry
from agentsync.config import AgentSyncConfig, ConfigManager, SyncOptions
from agentsync.discovery import discover_tools, merge_discovery_into_config
from agentsync.engine.autostart import resolve_daemon_command
from agentsync.engine.diagnostics import DiagnosticsEngine
from agentsync.engine.watcher import SyncWatcher, _create_daemon_logger
from agentsync.utils.atomic_io import atomic_write_json

DaemonLifecycleStatus = Literal[
    "running",
    "paused",
    "syncing",
    "waiting_close",
    "repairing",
    "stopped",
]


class DaemonState(BaseModel):
    """Live telemetry written to daemon_state.json for CLI and TUI Settings visibility."""

    pid: int = 0
    version: str = __version__
    executable: str = ""
    started_at_utc: str = ""
    updated_at_utc: str = ""
    status: DaemonLifecycleStatus = "stopped"
    power_profile: str = "eco"
    runtime_engine: str = "Pure Python"
    last_sync_utc: str | None = None
    sync_count: int = 0
    pending_sync_count: int = 0
    pending_repair_count: int = 0
    health_issues_count: int = 0
    cpu_percent: float = 0.0
    rss_mb: float = 0.0
    last_error: str | None = None
    watched_roots: list[str] = Field(default_factory=list)
    locked_apps: list[str] = Field(default_factory=list)


class SingleInstanceLock:
    """Cross-platform OS-level exclusive lockfile preventing duplicate daemon instances."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fp: Any | None = None

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Reclaim stale lock if PID is no longer alive
        if self.lock_path.exists():
            try:
                old = json.loads(self.lock_path.read_text(encoding="utf-8"))
                old_pid = int(old.get("pid") or 0)
                if old_pid and old_pid != os.getpid():
                    if psutil.pid_exists(old_pid):
                        try:
                            if psutil.Process(old_pid).status() != psutil.STATUS_ZOMBIE:
                                return False
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
            except Exception:
                pass

        fp = self.lock_path.open("a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                fp.seek(0)
                msvcrt.locking(fp.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fp.close()
            return False

        fp.seek(0)
        fp.truncate()
        fp.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "version": __version__,
                    "started_at_utc": datetime.now(UTC).isoformat(),
                }
            )
        )
        fp.flush()
        self._fp = fp
        return True

    def release(self) -> None:
        if self._fp is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fp.seek(0)
                msvcrt.locking(self._fp.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fp.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._fp.close()
        except Exception:
            pass
        self._fp = None
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _apply_low_priority(profile: str) -> None:
    """Lower OS scheduling priority in Eco / Battery-Safe profiles to keep PC load near zero."""
    if profile == "instant":
        return
    try:
        proc = psutil.Process(os.getpid())
        if sys.platform == "win32":
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            proc.nice(10)
    except Exception:
        pass


def _is_on_battery() -> bool:
    try:
        bat = psutil.sensors_battery()
        if bat is not None and bat.power_plugged is False:
            return True
    except Exception:
        pass
    return False


def _load_effective(config_path: Path | None, data_dir: Path | None) -> tuple[ConfigManager, AgentSyncConfig]:
    mgr = ConfigManager(config_path)
    cfg = mgr.load()
    if not cfg.tools:
        cfg = merge_discovery_into_config(cfg, discover_tools())
    if data_dir is not None:
        cfg.data_dir = str(data_dir)
    return mgr, cfg


def run_daemon_loop(
    config_path: Path | None = None,
    data_dir: Path | None = None,
    *,
    max_runtime_seconds: float | None = None,
) -> int:
    """Main single-instance daemon supervisor with hot-reload, auto-diagnostics, and exponential backoff."""
    mgr, cfg = _load_effective(config_path, data_dir)
    lock = SingleInstanceLock(cfg.daemon_lock_path)
    if not lock.acquire():
        return 0  # Another instance is already running

    logger = _create_daemon_logger(cfg.daemon_log_path)
    started_at = datetime.now(UTC).isoformat()
    self_proc = psutil.Process(os.getpid())
    self_proc.cpu_percent(interval=None)
    _apply_low_priority(cfg.power_profile)

    stop_requested = False

    def _on_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except Exception:
                pass

    def _write_state(
        status: DaemonLifecycleStatus,
        watcher: SyncWatcher | None = None,
        health_count: int = 0,
        last_error: str | None = None,
    ) -> None:
        snap = watcher.status_snapshot() if watcher is not None else None
        try:
            cpu = round(float(self_proc.cpu_percent(interval=None)), 2)
            rss_mb = round(self_proc.memory_info().rss / (1024 * 1024), 1)
        except Exception:
            cpu, rss_mb = 0.0, 0.0
        st = DaemonState(
            pid=os.getpid(),
            version=__version__,
            executable=sys.executable,
            started_at_utc=started_at,
            updated_at_utc=datetime.now(UTC).isoformat(),
            status=status,
            power_profile=cfg.power_profile,
            runtime_engine=runtime_engine_label(cfg.runtime_engine),
            last_sync_utc=snap.last_sync_utc if snap else None,
            sync_count=snap.sync_count if snap else 0,
            pending_sync_count=(len(snap.pending_queue) + len(snap.pending_tier1_queue)) if snap else 0,
            pending_repair_count=snap.pending_repair_count if snap else 0,
            health_issues_count=health_count,
            cpu_percent=cpu,
            rss_mb=rss_mb,
            last_error=last_error or (snap.last_error if snap else None),
            watched_roots=snap.watched_roots if snap else [],
            locked_apps=snap.running_locked_apps if snap else [],
        )
        try:
            atomic_write_json(cfg.daemon_state_path, st.model_dump(), DaemonState)
        except Exception:
            pass

    backoff = 2.0
    t0 = time.monotonic()
    last_diag_at = 0.0
    health_issues_count = 0
    cfg_mtime = mgr.config_path.stat().st_mtime if mgr.config_path.exists() else 0.0

    try:
        while not stop_requested:
            watcher: SyncWatcher | None = None
            try:
                mgr, cfg = _load_effective(config_path, data_dir)
                _apply_low_priority(cfg.power_profile)
                reg = get_registry()
                adapters = reg.create_enabled(cfg)
                opts = SyncOptions(
                    dry_run=False,
                    force=False,
                    include_subagents=cfg.include_subagents,
                    debounce_seconds=cfg.effective_debounce_seconds(),
                    strip_secrets=cfg.strip_secrets,
                    strict_vocab_guard=cfg.strict_vocab_guard,
                    skill_link_mode=cfg.skill_link_mode,
                )
                watcher = SyncWatcher(
                    cfg,
                    adapters,
                    options=opts,
                    daemon_mode=True,
                    run_startup_sync=cfg.realtime_sync_enabled,
                    config_path=mgr.config_path,
                )
                watcher.start()
                backoff = 2.0
                logger.info(
                    "Daemon running (pid=%s, version=%s, engine=%s, profile=%s).",
                    os.getpid(),
                    __version__,
                    runtime_engine_label(cfg.runtime_engine),
                    cfg.power_profile,
                )

                while not stop_requested:
                    if max_runtime_seconds is not None and (time.monotonic() - t0) >= max_runtime_seconds:
                        stop_requested = True
                        break

                    # 1. Hot-reload config.toml when user toggles Settings in TUI or CLI
                    if mgr.config_path.exists():
                        cur_mtime = mgr.config_path.stat().st_mtime
                        if cur_mtime != cfg_mtime:
                            cfg_mtime = cur_mtime
                            try:
                                _, new_cfg = _load_effective(config_path, data_dir)
                                new_adapters = reg.create_enabled(new_cfg)
                                cfg = new_cfg
                                _apply_low_priority(cfg.power_profile)
                                watcher.reload_config(new_cfg, new_adapters)
                            except Exception as exc:
                                logger.error("Hot-reload failed: %s", exc)

                    # 2. Process IPC control commands (`daemon pause|resume|stop|reload`)
                    if cfg.daemon_control_path.exists():
                        try:
                            ctrl = json.loads(cfg.daemon_control_path.read_text(encoding="utf-8"))
                            cfg.daemon_control_path.unlink(missing_ok=True)
                            cmd = str(ctrl.get("command") or "").lower()
                            if cmd == "stop":
                                stop_requested = True
                                break
                            if cmd == "pause":
                                watcher.pause()
                            elif cmd == "resume":
                                watcher.resume()
                            elif cmd == "reload":
                                _, new_cfg = _load_effective(config_path, data_dir)
                                cfg = new_cfg
                                watcher.reload_config(new_cfg, reg.create_enabled(new_cfg))
                            elif cmd == "repair_queued":
                                watcher.notify_repair_queued()
                        except Exception as exc:
                            logger.error("Control IPC error: %s", exc)

                    # 3. Battery-Safe automatic pause/resume
                    if cfg.power_profile == "battery_safe":
                        watcher.set_battery_paused(_is_on_battery())
                    else:
                        watcher.set_battery_paused(False)

                    # 4. Periodic low-priority Auto-Diagnostics (+ auto-queue safe heals if enabled)
                    now = time.monotonic()
                    interval_sec = max(900.0, cfg.diagnostics_interval_hours * 3600.0)
                    if cfg.auto_diagnostics_enabled and (last_diag_at == 0.0 or (now - last_diag_at) >= interval_sec):
                        last_diag_at = now
                        try:
                            diag = DiagnosticsEngine(cfg, watcher.adapters, config_path=mgr.config_path)
                            rep = diag.run_all(
                                verify_backups=False,
                                check_autostart=True,
                                check_split_install=False,
                                check_daemon=False,
                            )
                            health_issues_count = rep.actionable_count
                            if cfg.auto_heal_on_close and rep.fixable_count > 0:
                                diag.queue_repairs_on_close(rep.issues, safe_only=True)
                                watcher.notify_repair_queued()
                        except Exception as exc:
                            logger.error("Periodic diagnostics failed: %s", exc)

                    # 5. Update live daemon_state.json telemetry
                    snap = watcher.status_snapshot()
                    if snap.paused or snap.battery_paused:
                        st_label: DaemonLifecycleStatus = "paused"
                    elif snap.running_locked_apps and (snap.pending_queue or snap.pending_repair_count):
                        st_label = "waiting_close"
                    else:
                        st_label = "running"
                    _write_state(st_label, watcher=watcher, health_count=health_issues_count)
                    time.sleep(1.0)
            except Exception as exc:
                logger.error("Daemon loop crashed (%s). Backing off %.1fs...", exc, backoff)
                _write_state("stopped", watcher=watcher, health_count=health_issues_count, last_error=str(exc))
                if watcher is not None:
                    try:
                        watcher.stop()
                    except Exception:
                        pass
                time.sleep(backoff)
                backoff = min(60.0, backoff * 2.0)
            finally:
                if watcher is not None:
                    try:
                        watcher.stop()
                    except Exception:
                        pass
    finally:
        _write_state("stopped", watcher=None, health_count=health_issues_count)
        lock.release()
    return 0


# --- CLI & TUI Lifecycle Controllers ---


def read_daemon_status(config: AgentSyncConfig) -> DaemonState:
    """Read live daemon telemetry and verify whether the recorded PID is genuinely alive."""
    if config.daemon_state_path.exists():
        try:
            st = DaemonState.model_validate_json(config.daemon_state_path.read_text(encoding="utf-8"))
            alive = st.pid > 0 and psutil.pid_exists(st.pid)
            if alive:
                try:
                    p = psutil.Process(st.pid)
                    if p.status() == psutil.STATUS_ZOMBIE:
                        alive = False
                    else:
                        st.rss_mb = round(p.memory_info().rss / (1024 * 1024), 1)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    alive = False
            if not alive:
                st.status = "stopped"
                st.pid = 0
                st.cpu_percent = 0.0
                st.rss_mb = 0.0
            return st
        except Exception:
            pass
    return DaemonState(
        status="stopped",
        power_profile=config.power_profile,
        runtime_engine=runtime_engine_label(config.runtime_engine),
    )


def send_daemon_command(config: AgentSyncConfig, command: str) -> bool:
    """Send an asynchronous IPC control command to the running daemon."""
    st = read_daemon_status(config)
    if st.status == "stopped" or st.pid <= 0:
        return False
    atomic_write_json(
        config.daemon_control_path,
        {"command": command, "sent_at_utc": datetime.now(UTC).isoformat()},
    )
    return True


def start_daemon_background(
    config: AgentSyncConfig,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> DaemonState:
    """Spawn the native windowless single-instance daemon in the background."""
    cur = read_daemon_status(config)
    if cur.status != "stopped" and cur.pid > 0:
        return cur

    exe, args = resolve_daemon_command(config_path=config_path, data_dir=data_dir)
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([exe, *args], **kwargs)
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        st = read_daemon_status(config)
        if st.status != "stopped" and st.pid > 0:
            return st
        time.sleep(0.15)
    return read_daemon_status(config)


def stop_daemon(config: AgentSyncConfig, *, timeout: float = 5.0) -> bool:
    """Gracefully stop the background daemon via IPC, falling back to terminate if unresponsive."""
    st = read_daemon_status(config)
    if st.status == "stopped" or st.pid <= 0:
        config.daemon_lock_path.unlink(missing_ok=True)
        return False

    send_daemon_command(config, "stop")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(st.pid):
            return True
        time.sleep(0.15)

    try:
        proc = psutil.Process(st.pid)
        proc.terminate()
        proc.wait(timeout=3.0)
    except Exception:
        pass
    config.daemon_lock_path.unlink(missing_ok=True)
    return True


def restart_daemon(
    config: AgentSyncConfig,
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> DaemonState:
    stop_daemon(config)
    return start_daemon_background(config, config_path=config_path, data_dir=data_dir)


def main(argv: list[str] | None = None) -> None:
    """Entry point for `[project.gui-scripts] agentsyncw` and `pythonw -m agentsync.daemon`."""
    parser = argparse.ArgumentParser(prog="agentsyncw", description="AgentSync windowless background daemon.")
    parser.add_argument("--config", type=Path, default=None, help="Override config.toml path.")
    parser.add_argument("--data-dir", type=Path, default=None, help="Override agentsync data directory.")
    ns = parser.parse_args(argv)
    raise SystemExit(run_daemon_loop(config_path=ns.config, data_dir=ns.data_dir))


if __name__ == "__main__":
    main()
