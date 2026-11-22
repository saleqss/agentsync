# agentsync

Production-grade, bidirectional synchronization CLI for AI coding agents on Windows, macOS, and Linux.

`agentsync` keeps **Skills**, **MCP Servers**, **Plugins**, **Global & Project Instructions**, **Workspaces / Projects**, and **Chat Histories** synchronized across multiple AI coding tools using an isolated **Adapter <-> CanonicalModel <-> Adapter** architecture with strict idempotency, atomic validated writes, SHA-256 verified backups, and built-in schema diagnostics (`doctor` / `repair`).

---

## Supported Tools Matrix

| Tool | ID | Skills | MCP | Plugins | Instructions | Projects | Chats | Native Formats |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **OpenAI Codex** | `codex` | R/W | R/W | R/W | R/W | R/W | R/W | `config.toml`, `AGENTS.md`, `state_5.sqlite`, `thread_history_1.sqlite`, rollout JSONL |
| **Google Antigravity** | `antigravity` | R/W | R/W | R/W | R/W | R/W | R/W | `mcp_config.json`, `config.json`, `rules/*.md`, `conversation_summaries.db`, `agyhub_summaries_proto.pb`, `conversations/*.db` |
| **Claude Code** | `claude-code` | R/W | R/W | R/W | R/W | R/W | R/W | `~/.claude.json`, `CLAUDE.md`, `skills/`, `plugins/`, `projects/*.jsonl` |
| **OpenCode** | `opencode` | R/W | R/W | - | R/W | R/W | R/W | `opencode.json`, `AGENTS.md`, `skills/`, `opencode.db` |
| **DeepSeek Harness** | `deepseek-harness` | - | - | - | R/W (project) | R/W | R (v4 zstd) | `~/.dsh/storages/workspace.json`, `session.v4.jsonl.zstd` |

Additional adapters (Cursor, Windsurf, Cline, Gemini CLI, etc.) can be added as standalone modules via `@register_adapter` without touching core sync logic.

---

## Installation

### One-Line Installers

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/saleqss/agentsync/main/scripts/install.ps1 | iex
```

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/saleqss/agentsync/main/scripts/install.sh | bash
```

### Direct Package Managers

```bash
# uv (recommended)
uv tool install git+https://github.com/saleqss/agentsync.git

# pipx
pipx install git+https://github.com/saleqss/agentsync.git

# pip
pip install git+https://github.com/saleqss/agentsync.git
```

Verify global terminal availability:
```bash
agentsync --version
```

---

## Quickstart

```bash
# 1. Auto-detect installed tools and save persistent configuration
agentsync discover --save

# 2. Review enabled tools, paths, and capabilities
agentsync tools list

# 3. Validate existing native configs, SQLite stores, and session files
agentsync doctor

# 4. Preview initial migration from Codex without modifying any files
agentsync bootstrap --from codex --dry-run

# 5. Close running AI apps, then run initial migration (automatic SHA-256 backup created first)
agentsync bootstrap --from codex

# 6. Ongoing idempotent bidirectional sync (or run watch mode in background)
agentsync sync --dry-run
agentsync sync
agentsync watch --debounce 2.0
```

---

## CLI Reference

| Command | Description |
| :--- | :--- |
| `agentsync discover [--save]` | Scan standard OS paths, env vars, and `PATH` to detect installed tools |
| `agentsync tools list` | Show registered tools, paths, enabled status, and supported entities |
| `agentsync tools enable <tool>` | Enable a tool in persistent `config.toml` |
| `agentsync tools disable <tool>` | Disable a tool from synchronization |
| `agentsync tools set-path <tool> <path>` | Override the auto-detected config root for a tool |
| `agentsync tools set-entities <tool> <csv>` | Restrict synced entity categories per tool |
| `agentsync audit` | Read-only diagnostic summary of running processes and counts across enabled tools |
| `agentsync doctor [--fix] [--dry-run]` | Validate configs, SQLite integrity, protobuf hub summaries, and session JSONL schemas |
| `agentsync repair [--dry-run]` | Quarantine and regenerate broken rollouts / out-of-sync summary indexes |
| `agentsync bootstrap --from <tool> [--dry-run]` | Full backup -> clean reset of target skills/MCP/plugins -> import from source -> merge projects/rules/chats |
| `agentsync sync [--dry-run] [--backup]` | Strictly idempotent N-way bidirectional sync |
| `agentsync watch [--debounce 2.0]` | Debounced background watcher across all enabled tools |
| `agentsync status` | Display `sync_state.db` counts, backups, recent runs, and archived conflicts |
| `agentsync restore <backup_id>` | Roll back all tools and `sync_state.db` with full SHA-256 verification |

---

## Architecture & Safety Guarantees

- **Isolated Adapter Pattern:** `ToolAdapter <-> CanonicalModel (Pydantic v2) <-> ToolAdapter` keeps every tool's TOML, JSON, JSONL, SQLite, Protobuf, and zstd quirks strictly encapsulated.
- **Persistent Config:** Stored at `%APPDATA%\agentsync\config.toml` (Windows) or `~/.config/agentsync/config.toml` (macOS/Linux).
- **Local Sync State & Backups:** Stored at `%LOCALAPPDATA%\agentsync\` (Windows) or `~/.local/share/agentsync/` (macOS/Linux) containing `sync_state.db`, `backups/<id>/`, `conflicts/`, and `quarantine/`.
- **Strict Idempotency:** Running `agentsync sync` consecutively with no edits produces `0 diff` and touches zero target files.
- **Deduplication:**
  - **Projects:** Canonicalized via `os.path.realpath` (resolving symlinks and Windows NTFS junctions), stripping `\\?\` prefixes, decoding `file:///c%3A/...` URIs, and normalizing drive casing and separators.
  - **Chats:** Linked via `sync_state.db` (`chat_canonical` + `chat_links`) with deterministic SHA-256 fingerprinting (`project_key + normalized first messages`) for unseen sessions. Divergent edits use Last-Write-Wins (`updated_at`) and archive overwritten copies to `conflicts/`.
  - **Instructions:** Heading-aware Markdown block deduplication without duplicating shared sections.
- **Process Lock Guard:** Blocks write operations if target AI desktop apps (`Antigravity.exe`, `language_server.exe`, `ChatGPT.exe` under `OpenAI.Codex`, `codex.exe`, `opencode.exe`, `claude`, `DeepSeek Harness.exe`) are running, preventing SQLite/LevelDB corruption (overrideable with `--force`).
- **Atomic Validated Writes:** Every text/JSON/TOML/JSONL write validates syntax and schema to a temporary file, calls `fsync`, and swaps via `os.replace`.
- **Secret Redaction:** API keys, Bearer/Basic headers, JWTs, and URL credentials are automatically redacted in all CLI output, logs, and diff reports.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT License](LICENSE).
