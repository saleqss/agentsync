## Summary

- What does this PR change and why?
- Link related issue(s): Closes #

## Safety & Quality Checklist

- [ ] **100% English source code** (all code, identifiers, comments, docstrings, CLI help, and commit messages; non-English only in `src/agentsync/locales/{ru,zh}.json`).
- [ ] **Locale parity verified** (`en.json`, `ru.json`, `zh.json` have identical keys and format placeholders).
- [ ] **Zero schema guesswork** (adapter serializers match real native files verified by golden tests).
- [ ] **Safe I/O & SQLite protection** (atomic writes, `PRAGMA integrity_check`, WAL-safe snapshots, and process lock checks preserved).
- [ ] **Lint & Tests pass locally**:
  - `ruff check src tests`
  - `pytest -v`

## Verification Notes

Describe the test coverage added and any manual or TUI viewport checks performed (`80x24`, `120x30`, `180x50`).
