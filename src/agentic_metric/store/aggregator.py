"""Query layer: today sessions, range totals, breakdowns, heatmap, trend."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..formatting import source_label
from ..pricing import is_model_priced
from .database import Database


_USAGE_SOURCE = """(
    SELECT session_id,
           agent_type,
           provider,
           data_root,
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
           s.provider,
           s.data_root,
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
          AND u.provider = s.provider
          AND u.data_root = s.data_root
    )
)"""


def _usage_source(db: Database) -> str:
    """Return per-bucket usage plus a sessions fallback for pending re-syncs."""
    return _USAGE_SOURCE


def _session_count_expr(column: str = "session_id") -> str:
    return (
        "COUNT(DISTINCT agent_type || ':' || COALESCE(provider, '') || ':' || "
        f"COALESCE(data_root, '') || ':' || {column})"
    )


def _preferred_session_model_expr(session_alias: str = "s", usage_alias: str = "u") -> str:
    return f"""COALESCE(
        NULLIF(
            CASE
                WHEN {session_alias}.model = '<synthetic>' THEN ''
                ELSE COALESCE({session_alias}.model, '')
            END,
            ''
        ),
        MAX(CASE
            WHEN {usage_alias}.model NOT IN ('', '<synthetic>') THEN {usage_alias}.model
        END),
        ''
    )"""


def _model_label(model: str) -> str:
    """Return a display label, hiding unpriced model ids as Unknown."""
    model = model or ""
    if not model:
        return "(unknown)"
    if not is_model_priced(model):
        return "Unknown"
    return model


def _unknown_cost_expr(column: str = "estimated_cost_usd") -> str:
    return f"SUM(CASE WHEN {column} IS NULL THEN 1 ELSE 0 END)"


def get_today_sessions(db: Database) -> list[dict]:
    """Get all sessions from today (local timezone), ordered by started_at desc."""
    today = datetime.now().strftime("%Y-%m-%d")
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT u.session_id,
                   u.agent_type,
                   u.provider,
                   u.data_root,
                   COALESCE(NULLIF(s.project_path, ''), MAX(NULLIF(u.project_path, '')), '') AS project_path,
                   COALESCE(s.git_branch, '') AS git_branch,
                   {_preferred_session_model_expr()} AS model,
                   SUM(u.message_count) AS message_count,
                   SUM(u.user_turns) AS user_turns,
                   SUM(u.input_tokens) AS input_tokens,
                   SUM(u.output_tokens) AS output_tokens,
                   SUM(u.cache_read_tokens) AS cache_read_tokens,
                   SUM(u.cache_creation_tokens) AS cache_creation_tokens,
                   COALESCE(SUM(u.estimated_cost_usd), 0) AS estimated_cost_usd,
                   {_unknown_cost_expr("u.estimated_cost_usd")} AS unknown_cost_count,
                   COALESCE(s.started_at, '') AS started_at,
                   COALESCE(s.ended_at, '') AS ended_at,
                   COALESCE(s.first_prompt, '') AS first_prompt,
                   COALESCE(s.last_prompt, '') AS last_prompt
           FROM {usage} AS u
           LEFT JOIN sessions AS s
             ON s.session_id = u.session_id
            AND s.agent_type = u.agent_type
            AND s.provider = u.provider
            AND s.data_root = u.data_root
           WHERE u.usage_date = ?
           GROUP BY u.session_id, u.agent_type, u.provider, u.data_root
           ORDER BY started_at DESC
        """,
        (today,),
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        raw = row.get("model") or ""
        row["raw_model"] = raw
        row["model"] = _model_label(raw)
        out.append(row)
    return out


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
            label = f"{d.strftime('%b')} {d.day}"
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
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
        """,
        (from_date, to_date),
    ).fetchone()
    return dict(row) if row else {}


def get_range_by_agent(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-(agent, provider, data_root) aggregates within the range."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT agent_type,
                  provider,
                  data_root,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY agent_type, provider, data_root
           ORDER BY estimated_cost_usd DESC, unknown_cost_count DESC
        """,
        (from_date, to_date),
    ).fetchall()
    return [dict(r) for r in rows]


def _range_group_rows(
    db: Database,
    from_date: str,
    to_date: str,
    *,
    select_expr: str,
    group_expr: str,
    label_column: str,
) -> list[dict]:
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT {select_expr} AS {label_column},
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(message_count), 0) AS message_count,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY {group_expr}
           ORDER BY estimated_cost_usd DESC, unknown_cost_count DESC
        """,
        (from_date, to_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_range_by_host(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-host/source aggregates within the range."""
    rows = _range_group_rows(
        db,
        from_date,
        to_date,
        select_expr="data_root",
        group_expr="data_root",
        label_column="data_root",
    )
    sum_fields = (
        "session_count", "user_turns", "message_count", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "estimated_cost_usd", "unknown_cost_count",
    )
    merged: dict[str, dict] = {}
    for row in rows:
        host = source_label(row.get("data_root") or "")
        existing = merged.get(host)
        if existing is None:
            row["host"] = host
            merged[host] = row
            continue
        for field in sum_fields:
            existing[field] = (existing.get(field) or 0) + (row.get(field) or 0)

    return sorted(
        merged.values(),
        key=lambda r: (r.get("estimated_cost_usd") or 0, r.get("unknown_cost_count") or 0),
        reverse=True,
    )


def get_range_by_agent_type(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-agent aggregates within the range."""
    return _range_group_rows(
        db,
        from_date,
        to_date,
        select_expr="agent_type",
        group_expr="agent_type",
        label_column="agent_type",
    )


def get_range_by_provider(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-provider aggregates within the range."""
    return _range_group_rows(
        db,
        from_date,
        to_date,
        select_expr="CASE WHEN provider = '' THEN '—' ELSE provider END",
        group_expr="provider",
        label_column="provider",
    )


def get_range_by_model(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-model aggregates within the range."""
    rows = _range_group_rows(
        db,
        from_date,
        to_date,
        select_expr="model",
        group_expr="model",
        label_column="raw_model",
    )
    out = []
    for r in rows:
        row = dict(r)
        raw = row.pop("raw_model") or ""
        row["raw_model"] = raw
        row["model"] = _model_label(raw)
        out.append(row)
    return out


def get_range_by_agent_model(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-(agent, provider, data_root, model) aggregates within the range.

    Models reported as empty string are kept under ``"(unknown)"`` for clarity.
    """
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT agent_type,
                  provider,
                  data_root,
                  model AS raw_model,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY agent_type, provider, data_root, model
           ORDER BY agent_type, provider, data_root, estimated_cost_usd DESC, unknown_cost_count DESC
        """,
        (from_date, to_date),
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        raw = row.pop("raw_model") or ""
        row["raw_model"] = raw
        row["model"] = _model_label(raw)
        out.append(row)
    return out


def _range_by_key_model(
    db: Database,
    from_date: str,
    to_date: str,
    *,
    select_expr: str,
    group_expr: str,
    key_name: str,
) -> list[dict]:
    """Per-(key, model) aggregates: one row per ``group_expr`` × model."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT {select_expr} AS {key_name},
                  model AS raw_model,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(message_count), 0) AS message_count,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY {group_expr}, model
           ORDER BY estimated_cost_usd DESC, unknown_cost_count DESC
        """,
        (from_date, to_date),
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        raw = row.pop("raw_model") or ""
        row["raw_model"] = raw
        row["model"] = _model_label(raw)
        out.append(row)
    return out


def get_range_by_provider_model(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-(provider, model) aggregates within the range.

    Surfaces how a single model splits across billing channels — the same
    model can run through different providers at different prices — a view
    the wide source×agent×provider×model table fragments across hosts and
    agents.
    """
    return _range_by_key_model(
        db,
        from_date,
        to_date,
        select_expr="CASE WHEN provider = '' THEN '—' ELSE provider END",
        group_expr="provider",
        key_name="provider",
    )


def get_range_by_agent_type_model(db: Database, from_date: str, to_date: str) -> list[dict]:
    """Return per-(agent, model) aggregates within the range."""
    return _range_by_key_model(
        db,
        from_date,
        to_date,
        select_expr="agent_type",
        group_expr="agent_type",
        key_name="agent_type",
    )


def get_range_by_project_model(
    db: Database, from_date: str, to_date: str, limit: int = 10
) -> list[dict]:
    """Return top per-(project, model) rows, merged by source like by_project."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT data_root,
                  CASE WHEN project_path = '' THEN '(unspecified)'
                       ELSE project_path END AS project_path,
                  model AS raw_model,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY data_root, project_path, model
        """,
        (from_date, to_date),
    ).fetchall()

    sum_fields = (
        "session_count", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens",
        "estimated_cost_usd", "unknown_cost_count",
    )
    merged: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        row = dict(r)
        raw = row.pop("raw_model") or ""
        row["raw_model"] = raw
        row["model"] = _model_label(raw)
        key = (
            source_label(row.get("data_root") or ""),
            row["project_path"],
            row["model"],
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            continue
        for field in sum_fields:
            existing[field] = (existing.get(field) or 0) + (row.get(field) or 0)

    out = sorted(
        merged.values(),
        key=lambda r: (r.get("estimated_cost_usd") or 0, r.get("unknown_cost_count") or 0),
        reverse=True,
    )
    return out[:limit]


def get_range_by_project(db: Database, from_date: str, to_date: str, limit: int = 10) -> list[dict]:
    """Return per-project aggregates within the given date range, sorted by cost desc.

    Rows are collapsed by ``(source, project_path)`` rather than full
    ``data_root``: multiple *local* roots that share a project path merge into
    one row, while local-vs-remote and distinct remote hosts stay separate.
    This matches how the source prefix is rendered (local shows no prefix,
    remote shows ``host:``), so two local roots never produce two visually
    identical rows. The ``LIMIT`` is applied after merging so it never drops a
    project that only ranks high once its roots are combined.
    """
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT data_root,
                  CASE WHEN project_path = '' THEN '(unspecified)'
                       ELSE project_path END AS project_path,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY data_root, project_path
        """,
        (from_date, to_date),
    ).fetchall()

    sum_fields = (
        "session_count", "user_turns", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_creation_tokens", "estimated_cost_usd",
        "unknown_cost_count",
    )
    merged: dict[tuple[str, str], dict] = {}
    for r in rows:
        row = dict(r)
        # source_label is invariant within a merge group, so keeping the
        # first-seen data_root preserves the rendered source prefix.
        key = (source_label(row.get("data_root") or ""), row["project_path"])
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            continue
        for field in sum_fields:
            existing[field] = (existing.get(field) or 0) + (row.get(field) or 0)

    out = sorted(
        merged.values(),
        key=lambda r: (r.get("estimated_cost_usd") or 0, r.get("unknown_cost_count") or 0),
        reverse=True,
    )
    return out[:limit]


def get_range_by_project_agent(
    db: Database,
    from_date: str,
    to_date: str,
    limit: int = 10,
) -> list[dict]:
    """Return top project/source rows split by agent within the range."""
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT data_root,
                  agent_type,
                  CASE WHEN project_path = '' THEN '(unspecified)'
                       ELSE project_path END AS project_path,
                  {_session_count_expr()} AS session_count,
                  COALESCE(SUM(message_count), 0) AS message_count,
                  COALESCE(SUM(user_turns), 0) AS user_turns,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                  COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY data_root, project_path, agent_type
        """,
        (from_date, to_date),
    ).fetchall()

    sum_fields = (
        "session_count", "message_count", "user_turns", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_creation_tokens",
        "estimated_cost_usd", "unknown_cost_count",
    )
    merged: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        row = dict(r)
        key = (
            source_label(row.get("data_root") or ""),
            row["project_path"],
            row["agent_type"],
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
            continue
        for field in sum_fields:
            existing[field] = (existing.get(field) or 0) + (row.get(field) or 0)

    out = sorted(
        merged.values(),
        key=lambda r: (r.get("estimated_cost_usd") or 0, r.get("unknown_cost_count") or 0),
        reverse=True,
    )
    return out[:limit]


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
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
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
    def _sum_tokens_row(r) -> int:
        return (
            (r["input_tokens"] or 0)
            + (r["output_tokens"] or 0)
            + (r["cache_read_tokens"] or 0)
            + (r["cache_creation_tokens"] or 0)
        )

    def _token_fields(r) -> dict:
        return {
            "input_tokens": (r["input_tokens"] if r else 0),
            "output_tokens": (r["output_tokens"] if r else 0),
            "cache_read_tokens": (r["cache_read_tokens"] if r else 0),
            "cache_creation_tokens": (r["cache_creation_tokens"] if r else 0),
        }

    now = datetime.now()
    today = now.date()
    usage = _usage_source(db)

    if focus == "today":
        day = today - timedelta(days=offset)
        day_s = day.strftime("%Y-%m-%d")
        rows = db.conn.execute(
            f"""SELECT printf('%02d', usage_hour) AS hr,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                      {_unknown_cost_expr()} AS unknown_cost_count,
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
                "unknown_cost_count": (r["unknown_cost_count"] if r else 0),
                "tokens": _sum_tokens_row(r) if r else 0,
                "session_count": (r["session_count"] if r else 0),
                **_token_fields(r),
            })
        return out

    if focus == "week":
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(weeks=offset)
        days = [start + timedelta(days=i) for i in range(7)]
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
                      {_unknown_cost_expr()} AS unknown_cost_count,
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
                "unknown_cost_count": (r["unknown_cost_count"] if r else 0),
                "tokens": _sum_tokens_row(r) if r else 0,
                "session_count": (r["session_count"] if r else 0),
                **_token_fields(r),
            })
        return out

    if focus == "month":
        # Focused year/month (shifted by offset months)
        y, m = now.year, now.month - offset
        while m <= 0:
            m += 12
            y -= 1
        month_start = datetime(y, m, 1).date()
        if m == 12:
            month_end = datetime(y + 1, 1, 1).date() - timedelta(days=1)
        else:
            month_end = datetime(y, m + 1, 1).date() - timedelta(days=1)

        # Walk Mondays within the month; each bucket is Mon→Sun clipped
        # to the month boundaries.
        first_monday = month_start - timedelta(days=month_start.weekday())
        weeks: list[tuple] = []
        cursor = first_monday
        week_num = 1
        while cursor <= month_end:
            wk_from = max(cursor, month_start)
            wk_to = min(cursor + timedelta(days=6), month_end)
            weeks.append((week_num, wk_from, wk_to))
            cursor += timedelta(weeks=1)
            week_num += 1

        out = []
        for wn, wf, wt in weeks:
            row = db.conn.execute(
                f"""SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                          COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                          COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                          {_unknown_cost_expr()} AS unknown_cost_count,
                          {_session_count_expr()} AS session_count
                   FROM {usage}
                   WHERE usage_date BETWEEN ? AND ?""",
                (wf.strftime("%Y-%m-%d"), wt.strftime("%Y-%m-%d")),
            ).fetchone()
            out.append({
                "label": f"W{wn}",
                "sublabel": f"{wf.strftime('%m-%d')} – {wt.strftime('%m-%d')}",
                "cost": row["cost"] if row else 0.0,
                "unknown_cost_count": row["unknown_cost_count"] if row else 0,
                "tokens": _sum_tokens_row(row) if row else 0,
                "session_count": row["session_count"] if row else 0,
                **_token_fields(row),
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


def _trend_window(unit: str, count: int) -> tuple[str, str]:
    """Return the inclusive ``(from_date, to_date)`` span covered by a trend.

    Matches ``get_trend``'s bucket math so per-provider totals over this window
    sum to the same grand total as the trend strip.
    """
    now = datetime.now()
    today = now.date()
    if unit == "day":
        from_d = today - timedelta(days=count - 1)
        to_d = today
    elif unit == "week":
        this_monday = today - timedelta(days=today.weekday())
        from_d = this_monday - timedelta(weeks=count - 1)
        to_d = this_monday + timedelta(days=6)
    elif unit == "month":
        y, m = now.year, now.month - (count - 1)
        while m <= 0:
            m += 12
            y -= 1
        from_d = datetime(y, m, 1).date()
        if now.month == 12:
            to_d = datetime(now.year + 1, 1, 1).date() - timedelta(days=1)
        else:
            to_d = datetime(now.year, now.month + 1, 1).date() - timedelta(days=1)
    else:
        raise ValueError(f"Unknown trend unit: {unit}")
    return from_d.strftime("%Y-%m-%d"), to_d.strftime("%Y-%m-%d")


def get_trend_provider_totals(db: Database, unit: str, count: int) -> list[dict]:
    """Return per-provider cost totals over the same window as ``get_trend``.

    Each row: ``{provider, estimated_cost_usd, unknown_cost_count}``, sorted by
    cost desc. An empty provider is labelled ``"—"``.
    """
    from_d, to_d = _trend_window(unit, count)
    usage = _usage_source(db)
    rows = db.conn.execute(
        f"""SELECT CASE WHEN provider = '' THEN '—' ELSE provider END AS provider,
                  COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
                  {_unknown_cost_expr()} AS unknown_cost_count
           FROM {usage}
           WHERE usage_date BETWEEN ? AND ?
           GROUP BY provider
           ORDER BY estimated_cost_usd DESC, unknown_cost_count DESC
        """,
        (from_d, to_d),
    ).fetchall()
    return [dict(r) for r in rows]
