"""Query layer: today overview, daily trends, model breakdown."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import DailyTrend, LiveSession, TodayOverview
from ..pricing import estimate_cost
from .database import Database


_USAGE_SOURCE = """(
    SELECT session_id,
           agent_type,
           usage_date,
           usage_hour,
           project_path,
           model,
           message_count,
           user_turns,
           input_tokens,
           output_tokens,
           cache_read_tokens,
           cache_creation_tokens,
           estimated_cost_usd
    FROM session_usage
    UNION ALL
    SELECT s.session_id,
           s.agent_type,
           date(s.started_at, 'localtime') AS usage_date,
           CAST(strftime('%H', s.started_at, 'localtime') AS INTEGER) AS usage_hour,
           s.project_path,
           s.model,
           s.message_count,
           s.user_turns,
           s.input_tokens,
           s.output_tokens,
           s.cache_read_tokens,
           s.cache_creation_tokens,
           s.estimated_cost_usd
    FROM sessions AS s
    WHERE NOT EXISTS (
        SELECT 1
        FROM session_usage AS u
        WHERE u.session_id = s.session_id
          AND u.agent_type = s.agent_type
    )
)"""


def _usage_source(db: Database) -> str:
    """Return per-bucket usage plus a sessions fallback for pending re-syncs."""
    return _USAGE_SOURCE


def _session_count_expr(column: str = "session_id") -> str:
    return f"COUNT(DISTINCT agent_type || ':' || {column})"


def get_today_overview(db: Database) -> TodayOverview:
    """Get aggregated stats for today across all agents (local timezone)."""
    today = datetime.now().strftime("%Y-%m-%d")
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT agent_type,
                   COUNT(DISTINCT session_id) AS session_count,
                   SUM(message_count) AS message_count,
                   SUM(user_turns) AS user_turns,
                   SUM(input_tokens) AS input_tokens,
                   SUM(output_tokens) AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cache_creation_tokens) AS cache_creation_tokens,
                   SUM(estimated_cost_usd) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date = ?
           GROUP BY agent_type
        """,
        (today,),
    ).fetchall()

    overview = TodayOverview(date=today)
    for row in rows:
        r = dict(row)
        at = r["agent_type"]
        overview.session_count += r["session_count"] or 0
        overview.message_count += r["message_count"] or 0
        overview.tool_call_count += r["user_turns"] or 0
        overview.input_tokens += r["input_tokens"] or 0
        overview.output_tokens += r["output_tokens"] or 0
        overview.cache_read_tokens += r["cache_read_tokens"] or 0
        overview.cache_creation_tokens += r["cache_creation_tokens"] or 0
        overview.estimated_cost_usd += r["estimated_cost_usd"] or 0
        overview.by_agent[at] = {
            "session_count": r["session_count"] or 0,
            "turns": r["user_turns"] or 0,
            "message_count": r["message_count"] or 0,
            "input_tokens": r["input_tokens"] or 0,
            "output_tokens": r["output_tokens"] or 0,
            "cost": r["estimated_cost_usd"] or 0,
        }
    return overview


def merge_live_into_overview(
    overview: TodayOverview,
    live_sessions: list[LiveSession],
    today_sessions: list[dict],
) -> None:
    """Merge live session data into overview so totals include active sessions.

    Handles two cases:
    - Sessions in DB today: add delta if live data is fresher.
    - Live sessions not in today's DB rows (e.g. started yesterday but still
      running): add full live values.
    """
    db_ids = {(s["session_id"], s["agent_type"]) for s in today_sessions}
    db_by_id = {(s["session_id"], s["agent_type"]): s for s in today_sessions}

    for ls in live_sessions:
        at = ls.agent_type or ""
        session_key = (ls.session_id, at)
        t_turns = ls.today_user_turns if ls.today_user_turns >= 0 else ls.user_turns
        t_msgs = ls.today_message_count if ls.today_message_count >= 0 else ls.message_count
        t_in = ls.today_input_tokens if ls.today_input_tokens >= 0 else ls.input_tokens
        t_out = ls.today_output_tokens if ls.today_output_tokens >= 0 else ls.output_tokens
        t_cr = ls.today_cache_read_tokens if ls.today_cache_read_tokens >= 0 else ls.cache_read_tokens
        t_cw = ls.today_cache_creation_tokens if ls.today_cache_creation_tokens >= 0 else ls.cache_creation_tokens
        t_cost = estimate_cost(ls.model, t_in, t_out, t_cr, t_cw)

        if session_key in db_ids:
            db_s = db_by_id[session_key]
            if ls.output_tokens > 0:
                d_msg = max(0, t_msgs - (db_s["message_count"] or 0))
                d_turns = max(0, t_turns - (db_s["user_turns"] or 0))
                d_in = max(0, t_in - (db_s["input_tokens"] or 0))
                d_out = max(0, t_out - (db_s["output_tokens"] or 0))
                d_cr = max(0, t_cr - (db_s["cache_read_tokens"] or 0))
                d_cw = max(0, t_cw - (db_s["cache_creation_tokens"] or 0))
                d_cost = max(0, t_cost - (db_s["estimated_cost_usd"] or 0))

                overview.message_count += d_msg
                overview.tool_call_count += d_turns
                overview.input_tokens += d_in
                overview.output_tokens += d_out
                overview.cache_read_tokens += d_cr
                overview.cache_creation_tokens += d_cw
                overview.estimated_cost_usd += d_cost

                if at in overview.by_agent:
                    ba = overview.by_agent[at]
                    ba["turns"] = ba.get("turns", 0) + d_turns
                    ba["message_count"] = ba.get("message_count", 0) + d_msg
                    ba["input_tokens"] = ba.get("input_tokens", 0) + d_in
                    ba["output_tokens"] = ba.get("output_tokens", 0) + d_out
                    ba["cost"] = ba.get("cost", 0) + d_cost
        else:
            if ls.user_turns == 0 and ls.output_tokens == 0:
                continue
            overview.session_count += 1
            overview.message_count += t_msgs
            overview.tool_call_count += t_turns
            overview.input_tokens += t_in
            overview.output_tokens += t_out
            overview.cache_read_tokens += t_cr
            overview.cache_creation_tokens += t_cw
            overview.estimated_cost_usd += t_cost

            ba = overview.by_agent.get(at)
            if ba:
                ba["session_count"] = ba.get("session_count", 0) + 1
                ba["turns"] = ba.get("turns", 0) + t_turns
                ba["message_count"] = ba.get("message_count", 0) + t_msgs
                ba["input_tokens"] = ba.get("input_tokens", 0) + t_in
                ba["output_tokens"] = ba.get("output_tokens", 0) + t_out
                ba["cost"] = ba.get("cost", 0) + t_cost
            else:
                overview.by_agent[at] = {
                    "session_count": 1,
                    "turns": t_turns,
                    "message_count": t_msgs,
                    "input_tokens": t_in,
                    "output_tokens": t_out,
                    "cost": t_cost,
                }


def merge_live_into_trends(
    trends: list[DailyTrend],
    live_sessions: list[LiveSession],
    today_sessions: list[dict],
) -> None:
    """Merge live session data into daily trends for today's entry.

    Same logic as merge_live_into_overview but operates on the trend list.
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # Find or create today's entry (trends are ordered DESC)
    today_trend = None
    for t in trends:
        if t.date == today:
            today_trend = t
            break
    if today_trend is None:
        today_trend = DailyTrend(date=today)
        trends.insert(0, today_trend)

    db_ids = {(s["session_id"], s["agent_type"]) for s in today_sessions}
    db_by_id = {(s["session_id"], s["agent_type"]): s for s in today_sessions}

    for ls in live_sessions:
        session_key = (ls.session_id, ls.agent_type or "")
        t_turns = ls.today_user_turns if ls.today_user_turns >= 0 else ls.user_turns
        t_msgs = ls.today_message_count if ls.today_message_count >= 0 else ls.message_count
        t_in = ls.today_input_tokens if ls.today_input_tokens >= 0 else ls.input_tokens
        t_out = ls.today_output_tokens if ls.today_output_tokens >= 0 else ls.output_tokens
        t_cr = ls.today_cache_read_tokens if ls.today_cache_read_tokens >= 0 else ls.cache_read_tokens
        t_cw = ls.today_cache_creation_tokens if ls.today_cache_creation_tokens >= 0 else ls.cache_creation_tokens
        t_cost = estimate_cost(ls.model, t_in, t_out, t_cr, t_cw)
        if session_key in db_ids:
            db_s = db_by_id[session_key]
            if ls.output_tokens > 0:
                today_trend.user_turns += max(0, t_turns - (db_s["user_turns"] or 0))
                today_trend.message_count += max(0, t_msgs - (db_s["message_count"] or 0))
                today_trend.input_tokens += max(0, t_in - (db_s["input_tokens"] or 0))
                today_trend.output_tokens += max(0, t_out - (db_s["output_tokens"] or 0))
                today_trend.cache_read_tokens += max(0, t_cr - (db_s["cache_read_tokens"] or 0))
                today_trend.cache_creation_tokens += max(0, t_cw - (db_s["cache_creation_tokens"] or 0))
                d_cost = max(0, t_cost - (db_s["estimated_cost_usd"] or 0))
                today_trend.estimated_cost_usd += d_cost
        else:
            if ls.user_turns == 0 and ls.output_tokens == 0:
                continue

            today_trend.session_count += 1
            today_trend.user_turns += t_turns
            today_trend.message_count += t_msgs
            today_trend.input_tokens += t_in
            today_trend.output_tokens += t_out
            today_trend.cache_read_tokens += t_cr
            today_trend.cache_creation_tokens += t_cw
            today_trend.estimated_cost_usd += t_cost


def get_daily_trends(db: Database, days: int = 30) -> list[DailyTrend]:
    """Get daily aggregated stats for the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT usage_date AS date,
                  {_session_count_expr()} AS session_count,
                  SUM(user_turns) AS user_turns,
                  SUM(message_count) AS message_count,
                  SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_creation_tokens) AS cache_creation_tokens,
                  SUM(estimated_cost_usd) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date >= ?
           GROUP BY usage_date
           ORDER BY date DESC
        """,
        (cutoff,),
    ).fetchall()

    return [
        DailyTrend(
            date=r["date"],
            session_count=r["session_count"] or 0,
            user_turns=r["user_turns"] or 0,
            message_count=r["message_count"] or 0,
            input_tokens=r["input_tokens"] or 0,
            output_tokens=r["output_tokens"] or 0,
            cache_read_tokens=r["cache_read_tokens"] or 0,
            cache_creation_tokens=r["cache_creation_tokens"] or 0,
            estimated_cost_usd=r["estimated_cost_usd"] or 0,
        )
        for r in rows
    ]


def get_model_breakdown(db: Database, days: int = 30) -> list[dict]:
    """Get token/cost breakdown by model for the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT model,
                  SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_creation_tokens) AS cache_creation_tokens,
                  SUM(estimated_cost_usd) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date >= ? AND model != ''
           GROUP BY model
           ORDER BY estimated_cost_usd DESC
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_today_sessions(db: Database) -> list[dict]:
    """Get all sessions from today (local timezone), ordered by started_at desc."""
    today = datetime.now().strftime("%Y-%m-%d")
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT u.session_id,
                   u.agent_type,
                   COALESCE(NULLIF(s.project_path, ''), MAX(NULLIF(u.project_path, '')), '') AS project_path,
                   COALESCE(s.git_branch, '') AS git_branch,
                   COALESCE(NULLIF(s.model, ''), MAX(NULLIF(u.model, '')), '') AS model,
                   SUM(u.message_count) AS message_count,
                   SUM(u.user_turns) AS user_turns,
                   SUM(u.input_tokens) AS input_tokens,
                   SUM(u.output_tokens) AS output_tokens,
                   SUM(u.cache_read_tokens) AS cache_read_tokens,
                   SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                   SUM(u.estimated_cost_usd) AS estimated_cost_usd,
                   COALESCE(s.started_at, '') AS started_at,
                   COALESCE(s.ended_at, '') AS ended_at,
                   COALESCE(s.first_prompt, '') AS first_prompt,
                   COALESCE(s.last_prompt, '') AS last_prompt
           FROM {usage} AS u
           LEFT JOIN sessions AS s
             ON s.session_id = u.session_id AND s.agent_type = u.agent_type
           WHERE u.usage_date = ?
           GROUP BY u.session_id, u.agent_type
           ORDER BY started_at DESC
        """,
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_top_projects(db: Database, limit: int = 10) -> list[dict]:
    """Get top projects by message count."""
    rows = db.conn.execute(
        """SELECT project_path,
                  COUNT(*) AS session_count,
                  SUM(message_count) AS total_messages,
                  SUM(estimated_cost_usd) AS total_cost
           FROM sessions
           WHERE project_path != ''
           GROUP BY project_path
           ORDER BY total_messages DESC
           LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Range-based queries (for CLI `report` and TUI) ──────────────────


def resolve_range(kind: str, offset: int = 0) -> tuple[str, str, str]:
    """Resolve a named range to ``(label, from_date, to_date)`` inclusive.

    ``kind`` is one of: ``today``, ``week``, ``month``.
    ``offset`` shifts the window backwards by that many units (days / weeks /
    months). ``offset=0`` is the current period.
    """
    now = datetime.now()

    if kind == "today":
        d = (now - timedelta(days=offset)).date()
        s = d.strftime("%Y-%m-%d")
        if offset == 0:
            label = "Today"
        elif offset == 1:
            label = "Yesterday"
        else:
            label = d.strftime("%b %-d")
        return (label, s, s)

    if kind == "week":
        this_monday = now.date() - timedelta(days=now.weekday())
        start_d = this_monday - timedelta(weeks=offset)
        end_d = start_d + timedelta(days=6)
        if offset == 0:
            end_d = now.date()  # don't show future dates for current week
            label = "This week"
        elif offset == 1:
            label = "Last week"
        else:
            label = f"{offset} weeks ago"
        return (label, start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d"))

    if kind == "month":
        y, m = now.year, now.month - offset
        while m <= 0:
            m += 12
            y -= 1
        start_d = datetime(y, m, 1).date()
        # last day of that month
        if m == 12:
            next_month = datetime(y + 1, 1, 1).date()
        else:
            next_month = datetime(y, m + 1, 1).date()
        end_d = next_month - timedelta(days=1)
        if offset == 0:
            end_d = now.date()
            label = "This month"
        elif offset == 1:
            label = "Last month"
        else:
            label = start_d.strftime("%b %Y")
        return (label, start_d.strftime("%Y-%m-%d"), end_d.strftime("%Y-%m-%d"))

    raise ValueError(f"Unknown range kind: {kind}")


def get_range_totals(db: Database, from_date: str, to_date: str) -> dict:
    """Return summary totals for sessions within ``[from_date, to_date]`` inclusive."""
    usage = _usage_source(db)
    row = db.conn.execute(
        f"""SELECT {_session_count_expr()} AS session_count,
                  COALESCE(SUM(message_count), 0) AS message_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
        """,
        (from_date, to_date),
    ).fetchone()
    return dict(row) if row else {}


def get_range_by_agent(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-agent aggregates within the given date range."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT agent_type,
                  COUNT(DISTINCT session_id) AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY agent_type
           ORDER BY estimated_cost_usd DESC
        """,
        (from_date, to_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_range_by_agent_model(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-(agent, model) aggregates within the given date range.

    Models reported as empty string are kept under ``"(unknown)"`` for clarity.
    """
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT agent_type,
                  CASE WHEN model = '' THEN '(unknown)' ELSE model END AS model,
                  COUNT(DISTINCT session_id) AS session_count,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY agent_type, model
           ORDER BY agent_type, estimated_cost_usd DESC
        """,
        (from_date, to_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_range_by_project(db: Database, from_date: str, to_date: str, limit: int = 10) -> list[dict]:
    """Return per-project aggregates within the given date range, sorted by cost desc."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT CASE WHEN project_path = '' THEN '(unspecified)'
                       ELSE project_path END AS project_path,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY project_path
           ORDER BY estimated_cost_usd DESC
           LIMIT ?
        """,
        (from_date, to_date, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_range_by_time_model(db: Database, from_date: str, to_date: str, limit: int = 10) -> list[dict]:
    """Return the most expensive local time buckets split by agent/model."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT usage_date,
                  usage_hour,
                  agent_type,
                  CASE WHEN model = '' THEN '(unknown)' ELSE model END AS model,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY usage_date, usage_hour, agent_type, model
           HAVING COALESCE(SUM(estimated_cost_usd), 0) > 0
           ORDER BY estimated_cost_usd DESC
           LIMIT ?
        """,
        (from_date, to_date, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_range_top_sessions(db: Database, from_date: str, to_date: str, limit: int = 10) -> list[dict]:
    """Return the most expensive sessions within the given date range."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT u.session_id,
                  u.agent_type,
                  COALESCE(NULLIF(s.project_path, ''), MAX(NULLIF(u.project_path, '')), '') AS project_path,
                  COALESCE(NULLIF(s.first_prompt, ''), '') AS first_prompt,
                  COALESCE(NULLIF(s.started_at, ''), '') AS started_at,
                  COALESCE(NULLIF(s.ended_at, ''), '') AS ended_at,
                  COALESCE(NULLIF(s.model, ''), '') AS model,
                  COALESCE(
                      GROUP_CONCAT(
                          DISTINCT CASE
                              WHEN COALESCE(u.estimated_cost_usd, 0) > 0
                               AND u.model NOT IN ('', '<synthetic>')
                              THEN u.model
                          END
                      ),
                      ''
                  ) AS models,
                  SUM(u.message_count) AS message_count,
                  SUM(u.user_turns) AS user_turns,
                  COALESCE(SUM(u.input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(u.output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(u.cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(u.cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(u.estimated_cost_usd), 0) AS estimated_cost_usd
           FROM {usage} AS u
           LEFT JOIN sessions AS s
             ON s.session_id = u.session_id AND s.agent_type = u.agent_type
           WHERE u.usage_date BETWEEN ? AND ?
           GROUP BY u.session_id, u.agent_type
           HAVING COALESCE(SUM(u.estimated_cost_usd), 0) > 0
           ORDER BY estimated_cost_usd DESC
           LIMIT ?
        """,
        (from_date, to_date, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_range_daily(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-day aggregates within the given date range (ascending)."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT usage_date AS date,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY usage_date
           ORDER BY date ASC
        """,
        (from_date, to_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_heatmap(
    db: Database,
    focus: str,
    offset: int = 0,
) -> list[dict]:
    """Return per-bucket cost + tokens for the heatmap strip.

    ``focus`` is one of ``today`` / ``week`` / ``month``. ``offset``
    shifts the window back by that many units so navigation can reuse
    the same function.

    Each returned dict contains: ``label``, ``cost``, ``tokens``,
    ``session_count``. Buckets with zero activity are included so the
    strip layout stays stable.
    """
    from datetime import datetime as _dt, timedelta as _td

    def _sum_tokens_row(r) -> int:
        return (
            (r["input_tokens"] or 0)
            + (r["output_tokens"] or 0)
            + (r["cache_read_tokens"] or 0)
            + (r["cache_creation_tokens"] or 0)
        )

    now = _dt.now()
    today = now.date()
    usage = _usage_source(db)

    if focus == "today":
        day = today - _td(days=offset)
        day_s = day.strftime("%Y-%m-%d")
        rows = db.conn.execute(
            f"""SELECT printf('%02d', usage_hour) AS hr,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                      {_session_count_expr()} AS session_count
               FROM {usage}
               WHERE usage_date = ?
               GROUP BY hr""",
            (day_s,),
        ).fetchall()
        seen = {r["hr"]: r for r in rows}
        out = []
        for h in range(24):
            key = f"{h:02d}"
            r = seen.get(key)
            out.append({
                "label": key,
                "cost": (r["cost"] if r else 0.0),
                "tokens": _sum_tokens_row(r) if r else 0,
                "session_count": (r["session_count"] if r else 0),
            })
        return out

    if focus == "week":
        this_monday = today - _td(days=today.weekday())
        start = this_monday - _td(weeks=offset)
        days = [start + _td(days=i) for i in range(7)]
        labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        from_d = days[0].strftime("%Y-%m-%d")
        to_d = days[-1].strftime("%Y-%m-%d")
        rows = db.conn.execute(
            f"""SELECT usage_date AS d,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                      {_session_count_expr()} AS session_count
               FROM {usage}
               WHERE usage_date BETWEEN ? AND ?
               GROUP BY d""",
            (from_d, to_d),
        ).fetchall()
        seen = {r["d"]: r for r in rows}
        out = []
        for day, label in zip(days, labels):
            key = day.strftime("%Y-%m-%d")
            r = seen.get(key)
            out.append({
                "label": label,
                "cost": (r["cost"] if r else 0.0),
                "tokens": _sum_tokens_row(r) if r else 0,
                "session_count": (r["session_count"] if r else 0),
            })
        return out

    if focus == "month":
        # Focused year/month (shifted by offset months)
        y, m = now.year, now.month - offset
        while m <= 0:
            m += 12
            y -= 1
        month_start = _dt(y, m, 1).date()
        if m == 12:
            month_end = _dt(y + 1, 1, 1).date() - _td(days=1)
        else:
            month_end = _dt(y, m + 1, 1).date() - _td(days=1)

        # Walk Mondays within the month; each bucket is Mon→Sun clipped
        # to the month boundaries.
        first_monday = month_start - _td(days=month_start.weekday())
        weeks: list[tuple] = []
        cursor = first_monday
        week_num = 1
        while cursor <= month_end:
            wk_from = max(cursor, month_start)
            wk_to = min(cursor + _td(days=6), month_end)
            weeks.append((week_num, wk_from, wk_to))
            cursor += _td(weeks=1)
            week_num += 1

        out = []
        for wn, wf, wt in weeks:
            row = db.conn.execute(
                f"""SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                          COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                          COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                          {_session_count_expr()} AS session_count
                   FROM {usage}
                   WHERE usage_date BETWEEN ? AND ?""",
                (wf.strftime("%Y-%m-%d"), wt.strftime("%Y-%m-%d")),
            ).fetchone()
            out.append({
                "label": f"W{wn}",
                "sublabel": f"{wf.strftime('%m-%d')} – {wt.strftime('%m-%d')}",
                "cost": row["cost"] if row else 0.0,
                "tokens": _sum_tokens_row(row) if row else 0,
                "session_count": row["session_count"] if row else 0,
            })
        return out

    raise ValueError(f"Unknown focus: {focus}")


def get_trend(db: Database, unit: str, count: int) -> list[tuple[str, float]]:
    """Return the last ``count`` buckets of cost, one per unit.

    ``unit`` is ``"day"``, ``"week"`` or ``"month"``. Missing buckets are
    filled with 0 so the returned list always has ``count`` entries
    (oldest → newest).
    """
    now = datetime.now()
    today = now.date()
    usage = _usage_source(db)

    if unit == "hour":
        # Today by hour. ``count`` is ignored — we always return 24 buckets.
        today_s = today.strftime("%Y-%m-%d")
        rows = db.conn.execute(
            f"""SELECT printf('%02d', usage_hour) AS hr,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost
               FROM {usage}
               WHERE usage_date = ?
               GROUP BY hr""",
            (today_s,),
        ).fetchall()
        seen = {r["hr"]: r["cost"] for r in rows}
        return [(f"{h:02d}", seen.get(f"{h:02d}", 0.0)) for h in range(24)]

    if unit == "day":
        buckets = [today - timedelta(days=i) for i in range(count - 1, -1, -1)]
        keys = [d.strftime("%Y-%m-%d") for d in buckets]
        labels = [d.strftime("%m-%d") for d in buckets]
        from_d = buckets[0].strftime("%Y-%m-%d")
        to_d = buckets[-1].strftime("%Y-%m-%d")
        rows = db.conn.execute(
            f"""SELECT usage_date AS bucket,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost
               FROM {usage}
               WHERE usage_date BETWEEN ? AND ?
               GROUP BY bucket""",
            (from_d, to_d),
        ).fetchall()
        seen = {r["bucket"]: r["cost"] for r in rows}
        return list(zip(labels, [seen.get(k, 0.0) for k in keys]))

    if unit == "week":
        # Each bucket is a Monday-aligned week. Build Mondays oldest → newest.
        this_monday = today - timedelta(days=today.weekday())
        mondays = [this_monday - timedelta(weeks=i) for i in range(count - 1, -1, -1)]
        buckets: list[tuple[str, str]] = []
        for m in mondays:
            sunday = m + timedelta(days=6)
            buckets.append((m.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")))
        labels = [m.strftime("%m-%d") for m in mondays]

        result: list[tuple[str, float]] = []
        for (wk_from, wk_to), label in zip(buckets, labels):
            row = db.conn.execute(
                f"""SELECT COALESCE(SUM(estimated_cost_usd), 0) AS cost
                   FROM {usage}
                   WHERE usage_date BETWEEN ? AND ?""",
                (wk_from, wk_to),
            ).fetchone()
            result.append((label, row["cost"] if row else 0.0))
        return result

    if unit == "month":
        # Build (year, month) pairs oldest → newest.
        months: list[tuple[int, int]] = []
        y, m = now.year, now.month
        for _ in range(count):
            months.append((y, m))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        months.reverse()
        labels = [f"{y % 100:02d}-{m:02d}" for (y, m) in months]

        result = []
        for (y, m) in months:
            start = datetime(y, m, 1).date()
            if m == 12:
                end = datetime(y + 1, 1, 1).date() - timedelta(days=1)
            else:
                end = datetime(y, m + 1, 1).date() - timedelta(days=1)
            row = db.conn.execute(
                f"""SELECT COALESCE(SUM(estimated_cost_usd), 0) AS cost
                   FROM {usage}
                   WHERE usage_date BETWEEN ? AND ?""",
                (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
            ).fetchone()
            result.append((labels[months.index((y, m))], row["cost"] if row else 0.0))
        return result

    raise ValueError(f"Unknown trend unit: {unit}")
