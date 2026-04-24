"""Query layer: today overview, daily trends, model breakdown."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import DailyTrend, LiveSession, TodayOverview
from ..pricing import estimate_cost, estimate_session_cost
from .database import Database


def get_today_overview(db: Database) -> TodayOverview:
    """Get aggregated stats for today across all agents (local timezone)."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.conn.execute(
        """SELECT agent_type,
                  COUNT(*) AS session_count,
                  SUM(message_count) AS message_count,
                  SUM(user_turns) AS user_turns,
                  SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_creation_tokens) AS cache_creation_tokens,
                  SUM(estimated_cost_usd) AS estimated_cost_usd
           FROM sessions
           WHERE date(started_at, 'localtime') = ?
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
    db_ids = {s["session_id"] for s in today_sessions}
    db_by_id = {s["session_id"]: s for s in today_sessions}

    for ls in live_sessions:
        cost = estimate_session_cost(ls)
        at = ls.agent_type or ""

        if ls.session_id in db_ids:
            db_s = db_by_id[ls.session_id]
            if ls.output_tokens > 0:
                d_msg = max(0, ls.message_count - (db_s["message_count"] or 0))
                d_turns = max(0, ls.user_turns - (db_s["user_turns"] or 0))
                d_in = max(0, ls.input_tokens - (db_s["input_tokens"] or 0))
                d_out = max(0, ls.output_tokens - (db_s["output_tokens"] or 0))
                d_cr = max(0, ls.cache_read_tokens - (db_s["cache_read_tokens"] or 0))
                d_cw = max(0, ls.cache_creation_tokens - (db_s["cache_creation_tokens"] or 0))
                d_cost = max(0, cost - (db_s["estimated_cost_usd"] or 0))

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
            # Use today-only values for cross-day sessions
            t_turns = ls.today_user_turns if ls.today_user_turns >= 0 else ls.user_turns
            t_msgs = ls.today_message_count if ls.today_message_count >= 0 else ls.message_count
            t_in = ls.today_input_tokens if ls.today_input_tokens >= 0 else ls.input_tokens
            t_out = ls.today_output_tokens if ls.today_output_tokens >= 0 else ls.output_tokens
            t_cr = ls.today_cache_read_tokens if ls.today_cache_read_tokens >= 0 else ls.cache_read_tokens
            t_cw = ls.today_cache_creation_tokens if ls.today_cache_creation_tokens >= 0 else ls.cache_creation_tokens
            t_cost = estimate_cost(ls.model, t_in, t_out, t_cr, t_cw)

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

    db_ids = {s["session_id"] for s in today_sessions}
    db_by_id = {s["session_id"]: s for s in today_sessions}

    for ls in live_sessions:
        if ls.session_id in db_ids:
            db_s = db_by_id[ls.session_id]
            if ls.output_tokens > 0:
                today_trend.user_turns += max(0, ls.user_turns - (db_s["user_turns"] or 0))
                today_trend.message_count += max(0, ls.message_count - (db_s["message_count"] or 0))
                today_trend.input_tokens += max(0, ls.input_tokens - (db_s["input_tokens"] or 0))
                today_trend.output_tokens += max(0, ls.output_tokens - (db_s["output_tokens"] or 0))
                today_trend.cache_read_tokens += max(0, ls.cache_read_tokens - (db_s["cache_read_tokens"] or 0))
                today_trend.cache_creation_tokens += max(0, ls.cache_creation_tokens - (db_s["cache_creation_tokens"] or 0))
                d_cost = max(0, estimate_session_cost(ls) - (db_s["estimated_cost_usd"] or 0))
                today_trend.estimated_cost_usd += d_cost
        else:
            if ls.user_turns == 0 and ls.output_tokens == 0:
                continue
            t_turns = ls.today_user_turns if ls.today_user_turns >= 0 else ls.user_turns
            t_msgs = ls.today_message_count if ls.today_message_count >= 0 else ls.message_count
            t_in = ls.today_input_tokens if ls.today_input_tokens >= 0 else ls.input_tokens
            t_out = ls.today_output_tokens if ls.today_output_tokens >= 0 else ls.output_tokens
            t_cr = ls.today_cache_read_tokens if ls.today_cache_read_tokens >= 0 else ls.cache_read_tokens
            t_cw = ls.today_cache_creation_tokens if ls.today_cache_creation_tokens >= 0 else ls.cache_creation_tokens
            t_cost = estimate_cost(ls.model, t_in, t_out, t_cr, t_cw)

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
    rows = db.conn.execute(
        """SELECT date(started_at, 'localtime') AS date,
                  COUNT(*) AS session_count,
                  SUM(user_turns) AS user_turns,
                  SUM(message_count) AS message_count,
                  SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_creation_tokens) AS cache_creation_tokens,
                  SUM(estimated_cost_usd) AS estimated_cost_usd
           FROM sessions
           WHERE date(started_at, 'localtime') >= ?
           GROUP BY date(started_at, 'localtime')
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
    rows = db.conn.execute(
        """SELECT model,
                  SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_creation_tokens) AS cache_creation_tokens,
                  SUM(estimated_cost_usd) AS estimated_cost_usd
           FROM sessions
           WHERE date(started_at, 'localtime') >= ? AND model != ''
           GROUP BY model
           ORDER BY estimated_cost_usd DESC
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_today_sessions(db: Database) -> list[dict]:
    """Get all sessions from today (local timezone), ordered by started_at desc."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.conn.execute(
        """SELECT session_id, agent_type, project_path, git_branch, model,
                  message_count, user_turns, input_tokens, output_tokens,
                  cache_read_tokens, cache_creation_tokens, estimated_cost_usd,
                  started_at, ended_at, first_prompt, last_prompt
           FROM sessions
           WHERE date(started_at, 'localtime') = ?
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
    row = db.conn.execute(
        """SELECT COUNT(*) AS session_count,
                  COALESCE(SUM(message_count), 0) AS message_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM sessions
           WHERE date(started_at, 'localtime') BETWEEN ? AND ?
        """,
        (from_date, to_date),
    ).fetchone()
    return dict(row) if row else {}


def get_range_by_agent(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-agent aggregates within the given date range."""
    rows = db.conn.execute(
        """SELECT agent_type,
                  COUNT(*) AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM sessions
           WHERE date(started_at, 'localtime') BETWEEN ? AND ?
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
    rows = db.conn.execute(
        """SELECT agent_type,
                  CASE WHEN model = '' THEN '(unknown)' ELSE model END AS model,
                  COUNT(*) AS session_count,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM sessions
           WHERE date(started_at, 'localtime') BETWEEN ? AND ?
           GROUP BY agent_type, model
           ORDER BY agent_type, estimated_cost_usd DESC
        """,
        (from_date, to_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_range_by_project(db: Database, from_date: str, to_date: str, limit: int = 10) -> list[dict]:
    """Return per-project aggregates within the given date range, sorted by cost desc."""
    rows = db.conn.execute(
        """SELECT CASE WHEN project_path = '' THEN '(unspecified)'
                       ELSE project_path END AS project_path,
                  COUNT(*) AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM sessions
           WHERE date(started_at, 'localtime') BETWEEN ? AND ?
           GROUP BY project_path
           ORDER BY estimated_cost_usd DESC
           LIMIT ?
        """,
        (from_date, to_date, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_range_daily(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-day aggregates within the given date range (ascending)."""
    rows = db.conn.execute(
        """SELECT date(started_at, 'localtime') AS date,
                  COUNT(*) AS session_count,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd
           FROM sessions
           WHERE date(started_at, 'localtime') BETWEEN ? AND ?
           GROUP BY date(started_at, 'localtime')
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

    if focus == "today":
        day = today - _td(days=offset)
        day_s = day.strftime("%Y-%m-%d")
        rows = db.conn.execute(
            """SELECT strftime('%H', started_at, 'localtime') AS hr,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                      COUNT(*) AS session_count
               FROM sessions
               WHERE date(started_at, 'localtime') = ?
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
            """SELECT date(started_at, 'localtime') AS d,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                      COUNT(*) AS session_count
               FROM sessions
               WHERE date(started_at, 'localtime') BETWEEN ? AND ?
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
                """SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                          COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                          COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                          COUNT(*) AS session_count
                   FROM sessions
                   WHERE date(started_at, 'localtime') BETWEEN ? AND ?""",
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

    if unit == "hour":
        # Today by hour. ``count`` is ignored — we always return 24 buckets.
        today_s = today.strftime("%Y-%m-%d")
        rows = db.conn.execute(
            """SELECT strftime('%H', started_at, 'localtime') AS hr,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost
               FROM sessions
               WHERE date(started_at, 'localtime') = ?
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
            """SELECT date(started_at, 'localtime') AS bucket,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost
               FROM sessions
               WHERE date(started_at, 'localtime') BETWEEN ? AND ?
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
                """SELECT COALESCE(SUM(estimated_cost_usd), 0) AS cost
                   FROM sessions
                   WHERE date(started_at, 'localtime') BETWEEN ? AND ?""",
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
                """SELECT COALESCE(SUM(estimated_cost_usd), 0) AS cost
                   FROM sessions
                   WHERE date(started_at, 'localtime') BETWEEN ? AND ?""",
                (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")),
            ).fetchone()
            result.append((labels[months.index((y, m))], row["cost"] if row else 0.0))
        return result

    raise ValueError(f"Unknown trend unit: {unit}")
