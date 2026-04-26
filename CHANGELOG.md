# Changelog

## v0.3.4 (2026-04-26)

### Improvements

- **Auto-refresh key & active indicator**: the TUI auto-refresh toggle
  is now bound to plain `R` (no Shift prefix in the footer label) and
  visibly highlights while running — the key cell turns green and the
  "Auto" label becomes bold yellow, so you can tell at a glance whether
  the 30-second sync is active.

## v0.3.3 (2026-04-26)

### Improvements

- **TUI auto-refresh toggle**: press `Shift+R` to toggle a 30-second
  auto-sync in addition to the existing 5-minute cadence. Lowercase `r`
  continues to trigger a one-shot manual sync.

### Removed

- **Kimi and GLM pricing**: the Moonshot Kimi and Zhipu GLM builtin
  pricing rows were removed along with the "Others" section in both
  READMEs, since these providers are not reachable through the supported
  Claude Code / Codex collectors.

## v0.3.2 (2026-04-25)

### Improvements

- **Height-aware model fold**: the TUI breakdown panel now uses the
  available widget height to decide how many models to show per agent
  group, instead of a hard-coded limit of 4.
- **Ctrl+C no longer quits**: Ctrl+C is intercepted with a "Press q to
  quit" hint, so terminal-native copy (Cmd+C / Ctrl+Shift+C / Ctrl+C
  with selection) works across macOS, Linux, and Windows.
- **Cleaner fold line**: the `+N more models` row no longer prints a
  redundant `total` token count.

## v0.3.1 (2026-04-25)

### Improvements

- **Unknown model identity**: unknown models now display as
  `Unknown: <raw-model-id>` (e.g. `Unknown: gpt-5.4-pro`) in both TUI
  and CLI, so users can see which actual model IDs lack pricing.
- **Unknown model sort order**: unknown models no longer appear at the top
  of breakdowns; they are sorted after known models but before the
  collapsed "+N more models" fold, ensuring they remain visible without
  dominating the view.

## v0.3.0 (2026-04-25)

### Improvements

- **Documentation rewrite**: both README.md and README-CN.md have been
  restructured with full CLI option documentation, pricing sub-command
  reference (long-context, cache-duration), builtin model pricing tables,
  architecture overview, and data flow diagram.
- **Formatting module extracted**: pure formatting helpers (`fmt_cost`,
  `fmt_tokens`, `clip`, `short_path`, etc.) moved from `cli.py` to
  `formatting.py`, reducing `cli.py` by ~170 lines and making helpers
  independently testable and importable.
- **CI workflows**: added `.github/workflows/test.yml` (Python 3.10–3.13
  matrix on push/PR); `publish.yml` now runs tests before building.
- **Dev dependencies**: added `ruff` and `pytest-cov` to `[dev]` extras.

### Bug fixes

- **`--version` package name**: `agentic-metric --version` now queries the
  correct PyPI package name `agentic-metric-x` instead of `agentic-metric`,
  fixing a `PackageNotFoundError` when only the `-x` variant is installed.
- **`--range` date validation**: `report --range` now validates date format
  with `strptime` and rejects reversed ranges (`FROM > TO`) with a clear
  error instead of passing invalid dates to the database.
- **Pricing thread safety**: `_load_user_config` and `_save_user_config` are
  now protected by `threading.Lock`, preventing data races if called from
  background threads.
- **Self-referencing model alias removed**: the no-op entry
  `"gpt-5.1-codex-max": "gpt-5.1-codex-max"` has been removed from
  `_MODEL_ALIASES`.
- **Dead code removed**: the unused `big` parameter in `_stat()` has been
  removed.
- **Test reliability**: `test_store.py` no longer uses `tempfile.mktemp`
  (TOCTOU race); `test_pricing.py` uses an `autouse` fixture for cache
  reset consistency.

## v0.2.5 (2026-04-25)

### Bug fixes

- **Complete price configuration**: pricing overrides now use a structured
  config with separate model, long-context, and cache-duration rules.
- **Configurable long-context pricing**: added CLI commands to set, reset,
  disable, and re-enable request-size long-context prices without changing the
  normal model price.
- **Configurable cache duration pricing**: added CLI support for overriding
  observable 1-hour cache-write prices.
- **Fresh costs after pricing changes**: pricing changes now trigger history
  resync so event-level costs such as long-context requests are recalculated
  from the original local JSONL data before reports are shown.

## v0.2.4 (2026-04-25)

### Bug fixes

- **Unsupported speed/priority billing removed**: local Codex, Claude Code, and
  Gemini-compatible histories do not expose reliable non-standard provider mode
  markers, so reports now group by model only and do not price those modes
  separately.
- **Pricing cleanup**: removed the stored `service_tier` dimension and the old
  fast-mode multipliers; unknown models still display as `Unknown` with cost
  `?` until explicit pricing is configured.
- **GPT-5.5 long-context pricing**: added request-size pricing for GPT-5.5
  when event-level usage crosses the long-context threshold.

## v0.2.3 (2026-04-25)

### Bug fixes

- **Claude Code history on Windows locales**: read Claude JSONL `cwd` fields
  and `sessions-index.json` as UTF-8 so Windows non-UTF-8 system locales do
  not silently prevent `~/.claude` history from syncing.

## v0.2.2 (2026-04-25)

Pricing and platform compatibility fixes for the v0.2.x fork.

### Bug fixes

- **Provider-specific billing**: removed default/family pricing fallbacks.
  Unknown models now surface as `Unknown` with cost `?` until explicit
  pricing is configured.
- **Codex/OpenAI token accounting**: cached input is no longer charged twice;
  provider speed/priority modes are not priced separately, and OpenAI/Gemini
  long-context rates are only applied when event-level usage is available.
- **Claude cache accounting**: cache-read and cache-write tokens remain
  separate from input tokens, and observable 1-hour cache writes use the
  Anthropic 1-hour cache multiplier.
- **Codex code review pricing**: `codex-auto-review` is mapped to
  `gpt-5.3-codex`, matching OpenAI's Codex rate-card note that Code Review
  uses GPT-5.3-Codex.
- **Stored-cost repricing**: historical rows with collector-computed
  event-level costs are preserved across pricing fingerprint migrations, while
  aggregate-only rows are repriced without triggering request-size rates.
- **Windows support**: process detection now uses `psutil`/`tasklist`
  fallbacks, CWD matching normalizes Windows paths, date formatting avoids
  POSIX-only flags, and the app data directory uses `%LOCALAPPDATA%`.

## v0.2.1 (2026-04-24)

Follow-up correctness fixes on top of v0.2.0.

### Bug fixes

- **Usage billing attribution**: per-session rows are now split into
  `session_usage` per-day buckets so today/week/month rollups no longer
  over-count cross-day sessions. `report` and the TUI both read from the
  new bucketed view.
- **Codex forked session accounting**: when Codex resumes or forks a
  session it appends a new JSONL file that replays prior turns; the
  collector now dedupes replayed events so the forked session does not
  double-count the parent session's tokens.
- **Backend usage accounting**: Claude Code and Codex collectors now
  re-emit historical session totals on every sync (instead of only new
  events), so pricing overrides and backfills consistently re-cost past
  sessions. Aggregator queries and a large batch of tests were updated
  alongside.

## v0.2.0 (2026-04-23)

Focused fork: supports **Codex** and **Claude Code** only, with
redesigned TUI, a unified `report` command, and several calculation
correctness fixes.

### Breaking changes

- Removed collectors: VS Code Copilot Chat, OpenCode, Qwen Code. If you
  still need these, stay on v0.1.8 upstream.
- Replaced `today` / `history` / `status` CLI commands with a single
  `report` command:
  `report [--today|--week|--month|--range FROM:TO]`.

### Bug fixes

- **Codex double-counting cached tokens**: OpenAI's `input_tokens` is
  the total (including cached). The collector now stores
  `input_tokens - cached_input_tokens`, so `estimate_cost` no longer
  charges cached tokens at both input and cache-read pricing. This
  dramatically lowers Codex session cost (observed -87% on a large
  session with heavy prompt cache).
- **Pricing prefix matching**: sort by prefix length descending, so
  `gpt-5.4-mini` matches its own entry rather than `gpt-5.4`.
- **Date aggregation timezone**: `date(started_at, 'localtime')` is
  used everywhere, fixing UTC-vs-local off-by-one day in today / week /
  month rollups.
- **File truncation recovery**: if a JSONL file shrinks (truncated or
  rewritten), the collector now re-parses from offset 0 instead of
  silently skipping.
- **`git_branch` upsert**: the branch column is now updated from later
  syncs, not permanently pinned by the first insert.
- **User-pricing I/O**: overrides are cached in memory keyed by file
  mtime, avoiding a disk read on every cost estimation.

### Pricing table

- Added: `claude-opus-4-7`, `gpt-5.4` / `-mini` / `-nano` / `-pro`,
  `gemini-3.1-pro`, `gemini-3.1-flash`.
- Family fallback now splits `gpt-5` from generic `gpt-` so modern
  5.x models pick up 5.x pricing.

### TUI redesign

- Top row: three summary cells — TODAY / WEEK / MONTH — switched with
  `t` / `w` / `m`.
- Active-now table stays compact; dim rows show today's idle sessions.
- 30-day cost bar chart under the active table.
- Agent × model nested breakdown with proportion bars, driven by the
  currently-focused time range.
- Subtle panel borders, muted titles, yellow cost highlights.

## v0.1.8 (upstream baseline)

- Multiple live sessions in the same directory detected separately.
- Fix closed sessions being marked active via VS Code-specific fallback.
