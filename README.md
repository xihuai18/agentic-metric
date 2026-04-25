# Agentic Metric X

[![PyPI](https://img.shields.io/pypi/v/agentic-metric-x)](https://pypi.org/project/agentic-metric-x/)
[![Python](https://img.shields.io/pypi/pyversions/agentic-metric-x)](https://pypi.org/project/agentic-metric-x/)

[中文文档](README-CN.md)

A local-only monitoring tool for AI coding agents — like `top`, but for your coding agents. Track token usage and costs across **Claude Code** and **Codex** — with a TUI dashboard and CLI.

**Supported platforms: Linux, macOS, and Windows.**

**All data stays on your machine. No network requests, no telemetry, no data leaves your computer.** The tool only reads local agent data files (`~/.claude/`, `~/.codex/`) and process info.

![Agentic Metric TUI](agentic-metric-screenshot.png)

## Features

- **Live monitoring** — Detect running agent processes, incremental JSONL session parsing
- **Cost estimation** — Per-model pricing table with CLI management, calculates API-equivalent costs
- **Unified report** — One `report` command for today / week / month / custom date range, with agent × model breakdown, top projects, top sessions, and hourly/daily/weekly heatmaps
- **TUI dashboard** — Terminal UI with live refresh, stacked summary cells, heatmap strip, 30-day cost chart, and agent × model breakdown
- **Multi-agent** — Plugin architecture; supports Claude Code and Codex today, extensible

## Agent Data Coverage

| Field | Claude Code | Codex |
|-------|:-----------:|:-----:|
| Session ID | ✓ | ✓ |
| Project path | ✓ | ✓ |
| Git branch | ✓ | ✓ |
| Model | ✓ | ✓ |
| Input tokens | ✓ | ✓ |
| Output tokens | ✓ | ✓ |
| Cache tokens | ✓ | ✓¹ |
| User turns | ✓ | ✓ |
| Message count | ✓ | ✓ |
| First/last prompt | ✓ | ✓ |
| Cost estimation | ✓ | ✓ |
| Live active status | ✓ | ✓ |

> ¹ Codex exposes cache-read tokens only; cache-write is not reported. Codex's
> `input_tokens` already includes cached tokens, so the collector stores
> `input_tokens − cached_input_tokens` to avoid double-charging at both input
> and cache-read pricing.

## Installation

Requires Python 3.10+.

```bash
pip install agentic-metric-x
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uvx agentic-metric-x              # Run directly without installing
uv tool install agentic-metric-x   # Or install persistently
uv tool upgrade agentic-metric-x   # Upgrade to latest version
```

## Usage

```bash
agentic-metric                       # Launch TUI dashboard (default when no command given)
agentic-metric tui                   # Launch TUI dashboard explicitly
agentic-metric sync                  # Force sync collectors to the local database
agentic-metric report --today        # Today's usage report
agentic-metric report --week         # This week (Mon → today)
agentic-metric report --month        # This month
agentic-metric report --range 2026-04-01:2026-04-23   # Custom date range
agentic-metric today                 # Shortcut for `report --today`
agentic-metric week                  # Shortcut for `report --week`
agentic-metric month                 # Shortcut for `report --month`
agentic-metric history -d 30         # Last N days (default 14)
agentic-metric pricing               # Manage model pricing
```

`report` shows a header with total cost / sessions / turns / tokens / cache-hit
rate, a delta vs. the previous equivalent period, a heatmap strip (hours for
`--today`, days for `--week`, weeks for `--month`), a 30-day cost chart, and
breakdowns by agent × model, top projects, top sessions, and time buckets.

### Pricing Management

Model pricing is used for cost estimation. Builtin pricing is included for
common models. You can add new models, override builtins, configure
long-context rates, and configure observable cache-duration rates via CLI.
Overrides are stored in `$DATA/agentic_metric/pricing.json`.

```bash
agentic-metric pricing list
agentic-metric pricing set deepseek-r2 -i 0.5 -o 2.0
agentic-metric pricing set claude-opus-4-7 -i 4.0 -o 20.0 -cr 0.4 -cw 5.0
agentic-metric pricing long-context set gpt-5.5 --threshold 270000 -i 10 -o 45 -cr 1 -cw 0
agentic-metric pricing long-context disable gpt-5.5
agentic-metric pricing long-context enable gpt-5.5
agentic-metric pricing cache set claude-sonnet-4 --write-1h 6
agentic-metric pricing reset deepseek-r2
agentic-metric pricing reset --all
```

Unknown models are not priced by default. They are displayed as `Unknown` with
cost `?` until you add explicit pricing with `agentic-metric pricing set`.
Provider speed/priority modes are not shown or priced separately because the
local history files do not expose reliable non-standard markers.

After a pricing change, the command resyncs local history so event-level costs
such as long-context requests are recalculated from the original JSONL data.

### TUI Keybindings

| Key | Action |
|-----|--------|
| `←` / `→` | Switch view (Today / Week / Month) |
| `↑` / `↓` | Move time range earlier / later |
| `.` | Jump back to "now" (reset offset) |
| `t` / `w` / `m` | Focus Today / Week / Month directly |
| `r` | Refresh data |
| `q` | Quit |

## Data Sources

Paths differ by platform. `$DATA` refers to:

| | Linux | macOS | Windows |
|--|-------|-------|---------|
| `$DATA` | `~/.local/share` | `~/Library/Application Support` | `%LOCALAPPDATA%` |

| Agent | Path | Data |
|-------|------|------|
| Claude Code | `~/.claude/projects/` | JSONL sessions, token usage, model, branch |
| Claude Code | `~/.claude/stats-cache.json` | Daily activity stats |
| Claude Code | Process detection | Running status, working directory |
| Codex | `~/.codex/sessions/` | JSONL sessions, token usage, model |
| Codex | Process detection | Running status, working directory |

Claude Code honors `CLAUDE_CONFIG_DIR` and Codex honors `CODEX_HOME` — if you
have relocated either agent's config directory, the collectors pick up the
environment variable automatically.

All aggregated data is stored locally in `$DATA/agentic_metric/data.db` (SQLite).

## Unsupported Agents

- **Cursor** — Cursor stopped writing token usage data (`tokenCount`) to its local `state.vscdb` database around January 2026 (approximately version 2.0.63+). All `inputTokens`/`outputTokens` values are now zero. Cursor has moved usage tracking to a server-side system. Since this tool is designed to be fully offline with no network requests, monitoring Cursor usage is not supported.
- **OpenCode / Qwen Code / VS Code Copilot Chat** — collectors for these
  agents existed up to v0.1.8 and were removed in v0.2.0 as this fork narrowed
  its focus to Claude Code and Codex. If you need them, stay on v0.1.8 from
  upstream.

## Privacy

- **Fully offline** — no network requests, no data sent anywhere
- **Read-only** — never modifies agent config or data files
- All stats stored in a local SQLite database
- Delete the data directory at any time to remove all data (`~/.local/share/agentic_metric/` on Linux, `~/Library/Application Support/agentic_metric/` on macOS, `%LOCALAPPDATA%\agentic_metric\` on Windows)

## Development

```bash
git clone https://github.com/xihuai18/agentic-metric
cd agentic-metric
pip install -e ".[dev]"
pytest
```

## License

MIT — see [LICENSE](LICENSE).

Forked from [MrQianjinsi/agentic-metric](https://github.com/MrQianjinsi/agentic-metric) (v0.1.8 upstream). See [CHANGELOG.md](CHANGELOG.md) for what changed in this fork.
