# Contributing to agentsync

Thank you for your interest in contributing to **agentsync**!

## Core Engineering Principles

1. **100% English everywhere:** Keep all source code, docstrings, comments, CLI text, logs, tests, documentation, and commit messages strictly in English.
2. **Never guess native schemas:** Before writing or modifying an adapter serializer, inspect real native configuration files and databases, and add anonymized golden-file conformance tests under `tests/fixtures/`.
3. **Strict idempotency:** Running `agentsync sync` multiple times in a row without changes must make zero file modifications and create zero duplicates.
4. **Atomic, validated writes:** Every config or session write must validate syntax/schema first and atomically swap via `os.replace`.
5. **Secret safety:** Never print API keys, Bearer tokens, or credentials in logs or CLI reports; always pass through `agentsync.utils.secrets`.

## Adding a New Tool Adapter

`agentsync` uses a plugin-style adapter registry (`Adapter <-> CanonicalModel <-> Adapter`). To add support for another AI tool (e.g., Cursor, Windsurf, Cline, Gemini CLI):

1. Create `src/agentsync/adapters/<tool_name>.py` implementing `ToolAdapter` and decorate the class with `@register_adapter`.
2. Declare accurate `ToolCapabilities` (set `chat_write=False` or `plugin_write=False` if write schemas are not yet verified).
3. Implement `backup_targets()`, `doctor()`, and (if applicable) `repair()`.
4. Add auto-detection in `src/agentsync/discovery.py` and process classification in `src/agentsync/utils/process_guard.py`.
5. Register the module import in `src/agentsync/adapters/registry.py`.
6. Add comprehensive unit and golden-file conformance tests under `tests/`.

## Development Setup

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\pip install -e ".[dev]"
# macOS / Linux:
./.venv/bin/pip install -e ".[dev]"

ruff check src tests
pytest -v
```
