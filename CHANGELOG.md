# Changelog

## v0.2.2 (2026-04-25)

Pricing and platform compatibility fixes for the v0.2.x fork.

### Bug fixes

- **Provider-specific billing**: removed default/family pricing fallbacks.
  Unknown models now surface as `Unknown` with cost `?` until explicit
  pricing is configured.
- **Codex/OpenAI token accounting**: cached input is no longer charged twice;
  per-request pricing handles Codex `fast` tier and OpenAI/Gemini long-context
  tiers only when event-level usage is available.
- **Claude cache accounting**: cache-read and cache-write tokens remain
  separate from input tokens, and observable 1-hour cache writes use the
  Anthropic 1-hour cache multiplier.
- **Codex code review pricing**: `codex-auto-review` is mapped to
  `gpt-5.3-codex`, matching OpenAI's Codex rate-card note that Code Review
  uses GPT-5.3-Codex.
- **Stored-cost repricing**: historical rows with collector-computed
  event-level costs are preserved across pricing fingerprint migrations, while
  aggregate-only rows are repriced without triggering request-size tiers.
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
