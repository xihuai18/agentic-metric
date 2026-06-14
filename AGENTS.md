# AGENTS.md

Single source of truth for repository guidance. `CLAUDE.md` points here.

## Build & Test

```bash
pip install -e ".[dev]"    # install with dev dependencies
pytest                     # run all tests
```

## Release

Releases are published to PyPI automatically via GitHub Actions (`.github/workflows/publish.yml`), triggered by pushing a version tag.

Steps:

1. Bump `version` in `pyproject.toml`
2. Commit with message: `Release vX.Y.Z: <summary>`
3. Tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin main --tags
   ```
4. CI runs tests and publishes to PyPI via trusted publishing

## Code Layout

```
src/agentic_metric/
  cli.py              # Typer CLI: report, today, week, month, history, sync, tui
  models.py           # Shared dataclasses: LiveSession, TodayOverview, DailyTrend
  pricing.py          # Per-model cost estimation
  formatting.py       # Pure formatting helpers (cost/tokens, source/host labels)
  config.py           # Database path, collector roots, and SSH remote specs
  collectors/
    claude_code.py    # Claude Code JSONL parser (sessions, usage buckets)
    codex.py          # Codex collector
    remote.py         # SSH-backed wrapper: mirror a remote root to cache, then parse
    _process.py       # Process detection
  store/
    database.py       # SQLite schema and upsert logic
    aggregator.py     # Query helpers: range totals, by-agent, heatmap, trends
  tui/
    app.py            # Textual TUI app
    widgets.py        # SummaryCell, PeriodicHeatmap, TrendBlocks, Breakdown
    help_screen.py    # Keybinding cheatsheet modal
    pricing_screen.py # Read-only pricing view
    styles.tcss       # Textual CSS
```

## Key Metrics

- **user_turns**: Human messages (excludes tool_result entries with `type=user`)
- **message_count**: user_turns + deduplicated assistant messages (by `msg_id`)
- **requests** (display only): `message_count - user_turns` = LLM request count

## Documentation

- README pricing tables are generated from `pricing.py`. After changing
  `_BUILTIN_PRICING`, run
  `UPDATE_README_PRICING=1 pytest tests/test_readme_pricing.py` to refresh both
  README files; the test otherwise guards against drift.
- The README screenshot is regenerated with
  `UPDATE_TUI_DEMO_PNG=1 pytest tests/test_tui_demo.py` from privacy-safe demo
  data.
