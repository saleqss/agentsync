# AgentSync

**Universal cross-platform synchronization, patching, context-handoff, and interactive TUI suite for AI coding agents.**

[![CI](https://github.com/saleqss/agentsync/actions/workflows/ci.yml/badge.svg)](https://github.com/saleqss/agentsync/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

`agentsync` keeps your **skills**, **MCP servers**, **plugins**, **instructions/workspace rules**, **projects/workspaces**, and **chat sessions** synchronized and healthy across both **desktop/IDE apps** and **terminal CLIs** — without breaking native schemas, losing JSONC comments, leaking secrets, or overwriting hand-tuned host adaptations.

---

## Highlights

- **Interactive 8-Screen Textual TUI (Default Experience):** Run `agentsync` with no arguments for a dark high-contrast terminal UI with live startup auto-scan, interactive `[x]` / `[ ]` app checkboxes, First-Run Auto-Setup Wizard, non-blocking 60 FPS progress bars, confirmation modals, and full keyboard + mouse support. Automatically degrades cleanly to headless CLI in non-TTY pipes, `NO_COLOR`, or CI environments.
- **Instant Live Localization (EN / RU / 中文):** Switch seamlessly between **English**, **Русский**, and **简体中文** anytime via `[L]`, the clickable header badge `[EN | RU | 中文]`, `--lang en|ru|zh`, or Settings — persisted to `config.toml`.
- **Auto-Patch vs. Manual Patch with Strict Checkbox Isolation:** Toggle `[Auto-Patch]` (preflight + SHA-256 backup + repair + sync) or `[Manual Patch]` (preview exact risk-tagged diff first). Unchecked `[ ]` apps are strictly excluded from all reads and writes.
- **Live Context & Session Handoff (`agentsync handoff`):** Transfer any active conversation (history, project path, active instructions, and skills) across agents, verified directly against the target's native DB on disk with content-hash deduplication.
- **Lossless Cross-Client Translation:**
  - Layered bidirectional semantic token & permission map (`terminology_map.json`) with **Strict-Vocab Guard** warnings for unmappable host-only tokens.
  - Cross-format env-var placeholder translation (`${VAR}` <-> `${env:VAR}` <-> `{env:VAR}`) without ever expanding secrets, plus opt-in `--strip-secrets`.
  - Comment-preserving JSONC patching for Zed, OpenCode, and VS Code configs.
- **Adaptive Cross-OS Engineering:** Native Windows 10/11 (PowerShell 5/7, Windows Terminal, cmd.exe), WSL2 (Windows <-> WSL path translation), macOS, Linux, SSH, and Docker. Adaptive skill linking (`auto`: symlink -> Windows NTFS junction -> atomic real copy fallback on `WinError 1314`).
- **Precision Safety & Auditability:**
  - Running-process lock guard (preventing SQLite/protobuf corruption while apps are open).
  - `--include` / `--exclude` glob selectors (`skills:code-*,mcp`), `--scope user|project|all`, opt-in `--prune` mirror mode.
  - Immutable per-item SHA-256 **Apply Ledger**, protected host-adapted skills tracking, SHA-256 versioned **Backup & Restore**, and self-contained **Offline HTML Visual Inventory Board** (`agentsync board`).

---

## Supported AI Coding Clients (11 Built-In Adapters)

| Client | Variant | Skills | MCP | Plugins | Instructions | Projects | Chats | Native Format Highlights |
|---|---|---:|---:|---:|---:|---:|---:|---|
| **OpenAI Codex** | Desktop + CLI | Yes | Yes | Yes | Yes | Yes | Yes | `config.toml`, `state_5.sqlite`, native `session_meta` rollout `.jsonl`, `~/.codex/skills` vs `~/.agents/skills` dedup |
| **Google Antigravity** | Desktop | Yes | Yes | Yes | Yes | Yes | Yes | `mcp_config.json`, `conversation_summaries.db`, `conversations/*.db` (`user_version=1`), `agyhub_summaries_proto.pb` |
| **Claude Code** | Desktop + CLI | Yes | Yes | Yes | Yes | Yes | Yes | `~/.claude.json`, personal + `plugins/cache/` skills, `CLAUDE.md`, broken symlink healer, JSONL sessions |
| **OpenCode** | Desktop + TUI | Yes | Yes | — | Yes | Yes | Yes | JSONC `opencode.json`/`jsonc` (`{env:VAR}`), `AGENTS.md`, SQLite `opencode.db` |
| **DeepSeek Harness** | Desktop | — | — | — | Yes | Yes | Read-only | `storages/workspace.json`, project `AGENTS.md`, `session.v4.jsonl.zstd` reader (`chat_write=False` honestly enforced) |
| **Zed Editor** | IDE | Yes | Yes | — | Yes | — | — | JSONC `settings.json` (`context_servers` + extension preservation), `agent.tool_permissions`, `AGENTS.md`/`.rules`, `~/.agents/skills` |
| **Gemini CLI** | CLI | Yes | Yes | — | Yes | — | — | `~/.gemini/settings.json` (`mcpServers`), `GEMINI.md`, `~/.gemini/skills` |
| **Cursor** | IDE | Yes | Yes | — | Yes | — | — | `~/.cursor/mcp.json` (`${env:VAR}`), `.cursor/rules/*.mdc`, `~/.cursor/skills` |
| **Windsurf** | IDE | — | Yes | — | Yes | — | — | `~/.codeium/windsurf/mcp_config.json` (`${env:VAR}`), `.windsurfrules` |
| **Cline** | IDE Extension | — | Yes | — | Yes | — | — | `cline_mcp_settings.json` (`${env:VAR}`), `.clinerules` |
| **VS Code / Copilot** | IDE | — | Yes | — | Yes | — | — | JSONC `Code/User/mcp.json` (`servers` + `${env:VAR}`), `.github/copilot-instructions.md` |

New tools can be added cleanly as isolated adapter modules via `@register_adapter` without touching core sync logic (see [CONTRIBUTING.md](CONTRIBUTING.md)).

---

## Installation

### Windows (PowerShell one-liner)
```powershell
irm https://raw.githubusercontent.com/saleqss/agentsync/main/scripts/install.ps1 | iex
```

### macOS / Linux / WSL2
```bash
curl -fsSL https://raw.githubusercontent.com/saleqss/agentsync/main/scripts/install.sh | bash
```

### Via `uv`, `pipx`, or `pip`
```bash
uv tool install git+https://github.com/saleqss/agentsync.git
# or
pipx install git+https://github.com/saleqss/agentsync.git
# or from source
pip install -e ".[dev]"
```

Verify global launcher availability from any directory:
```bash
agentsync --version
```

---

## Quick Start

### 1. Launch Interactive TUI (Recommended)
```bash
agentsync
# Or with an explicit language:
agentsync --lang ru
agentsync --lang zh
agentsync ui
```

**TUI Keyboard Shortcuts:**
- `1`–`8`: Switch screens (`Dashboard`, `Patch & Sync`, `Handoff`, `Watch`, `Tools`, `Health`, `Backups`, `Settings`)
- `Space` / Click: Toggle `[x]` / `[ ]` app checkboxes
- `A` / `N` / `R`: Select All / Deselect All / Re-Scan System
- `P`: Patch Selected Apps (`Auto-Patch` or `Manual Patch` preview)
- `M`: Toggle `Auto-Patch` <-> `Manual Patch` mode
- `H`: Open Context & Session Handoff Center
- `L`: Cycle language live (`EN` -> `RU` -> `中文`)
- `?` / `Q`: Built-in User Guide / Quit

### 2. Headless CLI Commands (100% Scriptable)
```bash
# Auto-detect installed clients & optionally persist config.toml
agentsync discover
agentsync discover --save

# Validate all native configs, SQLite/protobuf stores, symlinks, and Codex rollouts
agentsync doctor
agentsync repair --dry-run
agentsync repair

# Auto-Patch or Manual Patch specific targets with glob selectors
agentsync patch --manual --dry-run --targets codex,antigravity
agentsync patch --auto --targets codex,antigravity,zed --include "skills:*,mcp" --exclude "mcp:internal-*"

# Bidirectional sync with scope, secret stripping, or opt-in prune
agentsync sync --dry-run --scope all
agentsync sync --backup --strip-secrets

# Transfer a live chat session with project rules & skills verified directly on disk
agentsync handoff --from codex --to antigravity --dry-run
agentsync handoff --from codex --to antigravity --session <id> --open auto

# Unified workspace instruction sync (.agentsync/workspace.yaml)
agentsync workspace init --path .
agentsync workspace sync --path .

# Offline HTML Visual Inventory Board & scriptable skill inventory
agentsync board --open
agentsync list-skills --json
agentsync install-skill

# Continuous watch, status/ledger inspection, and SHA-256 verified restore
agentsync watch --debounce 2.0
agentsync status
agentsync restore <backup_id>
```

---

## Development & Testing

```bash
ruff check src tests
pytest -v
python -m build
```

## License

MIT — see [LICENSE](LICENSE).
