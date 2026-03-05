"""Query layer: today overview, daily trends, model breakdown."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..models import DailyTrend, LiveSession, TodayOverview
from ..pricing import estimate_session_cost
from .database import Database


def get_today_overview(db: Database) -> TodayOverview:
    """Get aggregated stats for today across all agents."""
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
           WHERE date(started_at) = ?
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
            if ls.user_turns == 0 and ls.output_tokens == 0:
                continue
            overview.session_count += 1
            overview.message_count += ls.message_count
            overview.tool_call_count += ls.user_turns
            overview.input_tokens += ls.input_tokens
            overview.output_tokens += ls.output_tokens
            overview.cache_read_tokens += ls.cache_read_tokens
            overview.cache_creation_tokens += ls.cache_creation_tokens
            overview.estimated_cost_usd += cost

            ba = overview.by_agent.get(at)
            if ba:
                ba["session_count"] = ba.get("session_count", 0) + 1
                ba["turns"] = ba.get("turns", 0) + ls.user_turns
                ba["message_count"] = ba.get("message_count", 0) + ls.message_count
                ba["input_tokens"] = ba.get("input_tokens", 0) + ls.input_tokens
                ba["output_tokens"] = ba.get("output_tokens", 0) + ls.output_tokens
                ba["cost"] = ba.get("cost", 0) + cost
            else:
                overview.by_agent[at] = {
                    "session_count": 1,
                    "turns": ls.user_turns,
                    "message_count": ls.message_count,
                    "input_tokens": ls.input_tokens,
                    "output_tokens": ls.output_tokens,
                    "cost": cost,
                }


def get_daily_trends(db: Database, days: int = 30) -> list[DailyTrend]:
    """Get daily aggregated stats for the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.conn.execute(
        """SELECT substr(started_at, 1, 10) AS date,
                  COUNT(*) AS session_count,
                  SUM(user_turns) AS user_turns,
                  SUM(message_count) AS message_count,
                  SUM(input_tokens) AS input_tokens,
                  SUM(output_tokens) AS output_tokens,
                  SUM(cache_read_tokens) AS cache_read_tokens,
                  SUM(cache_creation_tokens) AS cache_creation_tokens,
                  SUM(estimated_cost_usd) AS estimated_cost_usd
           FROM sessions
           WHERE substr(started_at, 1, 10) >= ?
           GROUP BY substr(started_at, 1, 10)
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
           WHERE substr(started_at, 1, 10) >= ? AND model != ''
           GROUP BY model
           ORDER BY estimated_cost_usd DESC
        """,
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_today_sessions(db: Database) -> list[dict]:
    """Get all sessions from today, ordered by started_at descending."""
    today = datetime.now().strftime("%Y-%m-%d")
    rows = db.conn.execute(
        """SELECT session_id, agent_type, project_path, git_branch, model,
                  message_count, user_turns, input_tokens, output_tokens,
                  cache_read_tokens, cache_creation_tokens, estimated_cost_usd,
                  started_at, ended_at, first_prompt, last_prompt
           FROM sessions
           WHERE date(started_at) = ?
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
