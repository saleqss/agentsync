# Changelog

All notable changes to **AgentSync** are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
