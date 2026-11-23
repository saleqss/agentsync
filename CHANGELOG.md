# Changelog

All notable changes to **AgentSync** are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.1] - 2026-11-23

### Fixed
- **Antigravity -> Codex Project Binding & Unified Sidebar Visibility:**
  - Fixed URI/Windows/Cyrillic path canonicalization (`file:///c%3A/...`, `%D0%A1...`, `/C:/...`, `win32:/mnt/c/...`, and extended `\\?\C:\...` prefixes) to resolve exact on-disk casing with uppercase drive letters and native backslashes.
  - Updated `CodexAdapter.write_projects()` and `write_chat()` to register all workspace IDs in `.codex-global-state.json -> electron-persisted-atom-state["unified-sidebar-project-order-v1"]` (`"codex:project:<legacy_id>"`), `state_5.sqlite` (`threads.project_id` + `threads.cwd`), `sqlite/codex.db` (`local_thread_catalog.project_id`, `thread_source = 'user'`, and `catalog_revision`), and `thread-project-assignments` while removing bound threads from `projectless-thread-ids`.
  - Added lossless in-place project re-binding (`preserve_existing_rollout`) so re-binding existing or native Codex threads updates SQLite and global-state project bindings without overwriting rich native rollout items (`world_state`, `additional_tools`, `tokens_used`, or `git_sha`).
  - Added `CODEX_UNBOUND_SYNCED_THREAD` to `CodexAdapter.doctor()` and `repair()` (and `SAFE_AUTO_HEAL_CODES` for `repair --on-close`) to re-bind previously synced unbound threads in place without duplicating chats.
- **Full Bidirectional Chat Transfer & Visibility in Google Antigravity (`Codex -> Antigravity`):**
  - Fixed a critical protobuf serialization bug in `encode_trajectory_metadata()` (`proto_wire.py`) where `project_id = "outside-of-project"` was omitted from wire field 18 (`trajectory_metadata_blob`), which caused Antigravity's Go language server to overwrite `conversation_summaries.project_id` to `""` (empty string) and hide general/non-project Codex chats in the Antigravity UI.
  - Updated `AntigravityAdapter.read_chats()` to fall back to `raw_summary` (wire fields 9 and 17), `conversations/<cid>.db -> trajectory_metadata_blob`, and `projects/*.json` when `workspace_uris` or `project_id` columns are empty.
  - Updated `AntigravityAdapter.write_chat()` to auto-register missing project directories, preserve rich native `steps` / `gen_metadata` / `executor_metadata` when re-binding existing conversations, write native folder basename `workspace` labels in `conversations_fts`, and checkpoint `conversation_summaries.db` WAL cleanly.
  - Added `AGY_UNBOUND_OR_MALFORMED_SUMMARY` to `AntigravityAdapter.doctor()` and `repair()` to heal conversations with empty `project_id = ""` or missing protobuf wire fields 18 and 28.
  - Updated `SyncEngine._sync_chats()` to propagate project bindings across tools and heal `needs_repair` sessions even when message text hashes match, while maintaining strict 0-diff idempotency on runs 2 and 3.

## [0.5.0] - 2026-11-23

### Fixed
- **Windows Autostart Error (`Cannot find script file "...agentsync-daemon.vbs"`) & MSIX Store Python Virtualization:**
  - Permanently removed all VBScript (`.vbs`) and `wscript.exe` generation from the codebase.
  - Added a native windowless `[project.gui-scripts]` entry point (`agentsyncw = "agentsync.daemon:main"`) with resolution priority `agentsyncw.exe` -> `pythonw.exe -m agentsync.daemon` -> `python.exe -m agentsync.daemon` (zero console flash, zero WSH/VBS dependency).
  - Replaced `schtasks /TR` command-line registration with UTF-16 XML Task Scheduler definitions (`schtasks /Create /XML`) staged outside MSIX-virtualized `%LOCALAPPDATA%` (`~/.agentsync/.agentsync-task.xml`), eliminating broken Cyrillic/Unicode paths and Store Python `LocalCache` visibility mismatches.
  - Added pre- and post-registration task verification plus automatic legacy launcher cleanup across Scheduled Tasks, `%LOCALAPPDATA%\agentsync`, Store Python `LocalCache`, Startup folder, and `HKCU\...\Run`.
- **Split-Installation Drift & Windows `WinError 32` Self-Update Lock:**
  - Added `detect_split_installations()` to detect version drift between global PATH `agentsync.exe`, active `.venv`, and the registered autostart target.
  - Replaced locked in-place Windows upgrades with a detached pure-PowerShell updater helper (zero `.vbs`) that waits for the parent PID to exit, stops the daemon cleanly, upgrades and unifies split installations, verifies `--version`, refreshes native autostart, restarts the daemon, and optionally relaunches the TUI.

### Added
- **Single-Instance Native Background Daemon (`agentsync daemon`):**
  - Added `agentsync daemon start | stop | restart | pause | resume | status | run` backed by OS-level locking (`msvcrt.locking` / `fcntl.flock` with stale dead-PID reclaim), live telemetry (`daemon_state.json`), IPC control (`daemon_control.json`), low process priority (`BELOW_NORMAL_PRIORITY_CLASS` / `nice(10)`), exponential backoff (`2s` to `60s`), and rotating `daemon.log`.
- **Continuous Auto-Diagnostics & Deferred Self-Healing (`repair --on-close`):**
  - Added `DiagnosticsEngine` checking autostart health, legacy `.vbs` leftovers, split-installation drift, daemon lock/log health, all 3 Codex SQLite DBs + Antigravity DBs/indexes, rollouts, configs, symlinks, duplicates, and backups.
  - Added clickable live header `[Health: OK / N issue(s)]` pill opening Screen 6 (`Auto-Diagnostics & Deferred Self-Healing`) with `[Fix Now]` and `[Auto-Fix on Close]`.
  - Added persistent `repair_queue.json` and `agentsync repair --on-close` so the background daemon automatically executes verified repairs ~2.5s after locking apps exit.
- **Split-Tier Real-Time Sync Engine & Ultra-Low Idle Footprint:**
  - Split filesystem events into **Tier 1** (unlocked skills, instructions, plugins, MCP configs — synced in real time even while AI apps are open) and **Tier 2** (SQLite DBs, protobuf stores, sessions — safely queued for `Sync-on-Close`).
  - Replaced continuous idle process polling with `threading.Condition` zero-idle sleep when both pending sync and repair queues are empty.
  - Added incremental `DirtyFileCache` (`path -> (mtime_ns, size, sha256)`) with short-window self-write suppression and configurable Power Profiles (`Eco` / `Instant` / `Battery-Safe`) that hot-reload live on `config.toml` changes.
- **Hot-Path Optimization & Optional Rust (`fast-anything` / PyO3) Boundary:**
  - Added `agentsync.accel` exposing an optional compiled Rust (`_agentsync_rs`) boundary with automatic `Pure Python` fallback (`FAST_ANYTHING_DISABLE=1` / `runtime_engine = "python"`).
  - Accelerated `SKILL.md` flat frontmatter parsing (~39x faster on 250-skill batches), compiled JSONC comment stripping (~1.6x faster), and fast streaming JSONL rollout validation (~10.6 ms per 2 MB rollout) in pure Python while maintaining bit-for-bit parity.
- **Complete Live Settings Center & CLI Configuration (`agentsync config`):**
  - Expanded TUI Screen 8 with a live Daemon telemetry card (Start / Stop / Restart / Open Log) plus live hot-reloading controls for Autostart, Real-Time Sync, Sync-on-Close, Auto-Diagnostics, Auto-Heal on Close, Power Profile, Debounce Delay, Patch Mode, Auto-Backup, Skill Link Strategy, Runtime Engine, Auto-Scan, Strip Secrets, Strict-Vocab, Update Policy/Channel, OS Notifications, and live EN/RU/ZH switching.
  - Added `agentsync config list | get <key> | set <key> <value>` with automatic daemon hot-reload.
  - Upgraded `UpdateModal` with `[Update Now]`, `[Remind Later]` (24h), and `[Skip Version]` persisted in `config.toml` alongside 6-hour cached release checks.

## [0.4.0] - 2026-11-22

### Fixed
- **Codex Desktop `disk I/O error` on `sqlite/codex.db`:**
  - Added full WAL/SHM-safe SQLite transactional protocol (`sqlite_transactional_edit`, `PRAGMA busy_timeout=5000`, `PRAGMA wal_checkpoint(TRUNCATE)`, `PRAGMA integrity_check` pre- and post-write, automatic rollback from verified `sqlite3.Connection.backup()` pre-write snapshots).
  - Included `~/.codex/sqlite/codex.db` in `backup_targets()`, `doctor()`, `repair()`, and backup restore (clearing stale `-wal`/`-shm`/`-journal` sidecars before atomic replacement).
  - Expanded process lock guard to catch ALL background helpers (`ChatGPT.exe`, `codex.exe`, `extension-host.exe` under `.codex`, `codex-computer-use-swift.exe`, `OpenAI.CodexPrimaryRuntime` bundled `node.exe`, `msedgewebview2.exe`, WSL `codex`, Antigravity helpers) while preserving safe exclusion of `codex-windows-sandbox-service.exe`.
- **Infinite loading spinner on Antigravity -> Codex synced chats:**
  - Matched native Codex Desktop thread hydration schema 1:1 across `state_5.sqlite` (`has_user_event=0`, native `cli_version`, `originator='Codex Desktop'`, `model`, `reasoning_effort`), `thread_history_1.sqlite` (exact turn byte offsets & compact `item_json`), `sqlite/codex.db` (`local_thread_catalog` upsert + `catalog_revision` bump), `session_index.jsonl`, and `rollout-*.jsonl` (`session_meta`, `task_started`, `turn_context`, `response_item`, `item_completed` with `phase='final_answer'`, `task_complete`).
  - Upgraded `doctor` and `repair` to detect and regenerate broken/legacy `agy-codex-sync` rollouts and corrupted turn byte offsets automatically.
- **Legacy Windows PowerShell (`conhost.exe`) `?` glyph rendering & narrow terminal clipping:**
  - Eliminated Textual's tall/hkey half-block borders and `▐`/`▌` checkbox glyphs globally (`AgentCheckbox` uses crisp `[✓]`/`[ ]` or pure ASCII `[X]`/`[ ]` in conhost).
  - Added fluid responsive `on_resize` breakpoints (`.compact` `<100`, `.medium` `100-139`, `.wide` `>=140`) with wrapping CSS grids and slim compact navigation rail down to `80x24`.
  - Fixed `Selected: X/Y` dashboard counter to always strictly match checked installed checkboxes 1:1.

### Added
- **Always-On Automatic Verified Backups & Retention:**
  - Enabled pre-write SHA-256 + SQLite `PRAGMA integrity_check` backups by default across `sync`, `patch`, `bootstrap`, `repair`, `handoff`, and background `watch` (zero redundant snapshots on 0-diff no-op runs).
  - Added retention pruning (`max_backups=20`, `max_backup_days=30`), `agentsync backup create`, `agentsync backup verify`, and TUI one-click snapshot + verify buttons.
- **Deep Repair Engine:**
  - Recovers damaged `codex.db` from verified `~/.codex/sqlite/db-backups/sqlite-*` auto-recovery snapshots, cleans orphaned `-wal`/`-shm` sidecars, auto-restores corrupted JSON/JSONC/TOML configs from the latest verified `agentsync` backup, heals broken symlinks, and deduplicates `~/.codex/skills` vs `~/.agents/skills`.
- **Unattended Autostart & Smart `Sync-on-Close` Daemon:**
  - Added `agentsync autostart enable | disable | status` (Windows hidden logon Scheduled Task without admin, macOS `launchd` LaunchAgent, Linux `systemd --user`) and `agentsync watch --daemon` with rotating `daemon.log`.
  - Added smart **Sync-on-Close deferred queue**: queues filesystem events safely while AI apps are open and automatically syncs after a configurable grace period once apps close.
  - Wired the real live `SyncWatcher` into TUI Screen 4 (`Watch`) with Start/Pause/Resume/Stop, watched roots, lock badges, pending queue count, and live scrolling log.
- **Built-In Self-Updater:**
  - Added `agentsync update [--check] [--force] [--pre]`, 12-hour cached non-blocking GitHub Releases check, install-method auto-detection (`uv`, `pipx`, `editable`, `pip`), clickable TUI header `[Update vX.Y.Z]` badge, and Settings Updates card.
- **Power-User Features:**
  - **Global Command Palette (`Ctrl+K`)** searching screens, actions, tools, skills, MCP servers, and chats.
  - **Interactive Diff Viewer Modal** when clicking any row in the Patch/Sync diff table.
  - **Portable Profile Bundles:** `agentsync bundle export <file.zip>` / `import <file.zip>` (secret-redacted by default, `--include-secrets` opt-in, SHA-256 manifest verification).
  - **Anonymized Diagnostics Bundle:** `agentsync support-bundle` (strictly strips secrets, emails, IP addresses, usernames, and all chat transcripts).
  - **Optional OS Notifications** (opt-in `notifications = false` by default).
- **GitHub Presentation:**
  - Added reproducible Textual SVG screenshots, Hero Banner, Social Preview, animated demo SVG, GitHub Issue/PR templates, and automated Release workflow.

## [0.3.0] - 2026-11-22

### Added
- 8-screen interactive Textual TUI with live EN / RU / ZH localization and 4-step First-Run Onboarding Wizard.
- Universal 11-client adapter matrix (Codex, Antigravity, Claude Code, OpenCode, DeepSeek Harness, Zed, Gemini CLI, Cursor, Windsurf, VS Code Copilot, Cline).
- Auto-Patch & Manual Patch engine with strict checkbox isolation, Apply Ledger (per-item SHA-256), `--include`/`--exclude` glob selectors, `--scope`, `--prune`, `--strip-secrets`, and strict-vocab guard.
- Live Context & Session Handoff engine (`agentsync handoff`) with evidence-based DB verification and content-hash deduplication.
- Offline HTML Visual Inventory Board (`agentsync board`), unified project workspace manifest (`agentsync workspace init|sync`), built-in `agentsync` agent skill (`agentsync install-skill`), and scriptable `agentsync list-skills --json`.

## [0.2.0] - 2026-11-22

### Added
- Refactored into open-source `agentsync` CLI package with 100% English source, global terminal launcher, schema-conformant Codex rollout v2 generator, native Antigravity protobuf chat serialization & index registration, `doctor`, and `repair`.

## [0.1.0] - 2026-11-22

### Added
- Initial Windows 11 local audit and bidirectional synchronization prototype between OpenAI Codex and Google Antigravity (`agy-codex-sync`).
