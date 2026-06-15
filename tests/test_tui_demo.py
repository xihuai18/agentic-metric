"""Regression test and generator for the README TUI demo screenshot."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.terminal_theme import MONOKAI

from agentic_metric.collectors import CollectorRegistry
from agentic_metric.store.database import Database
from agentic_metric.tui.app import AgenticMetricApp


DEMO_NOW = datetime(2026, 6, 14, 16, 30, 0)
REPO_ROOT = Path(__file__).resolve().parents[1]
README_SCREENSHOT = REPO_ROOT / "agentic-metric-screenshot.png"


class DemoDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return DEMO_NOW.replace(tzinfo=tz)
        return DEMO_NOW


class DemoCollectors(CollectorRegistry):
    """Deterministic collector registry for the screenshot demo."""

    def sync_all(self, db) -> None:
        return None

    def get_sync_errors(self) -> list[str]:
        return []


def _add_session(
    db: Database,
    *,
    session_id: str,
    agent_type: str,
    provider: str,
    data_root: str,
    project_path: str,
    model: str,
    buckets: list[dict],
) -> None:
    total = {
        "message_count": sum(int(b.get("message_count") or 0) for b in buckets),
        "user_turns": sum(int(b.get("user_turns") or 0) for b in buckets),
        "input_tokens": sum(int(b.get("input_tokens") or 0) for b in buckets),
        "output_tokens": sum(int(b.get("output_tokens") or 0) for b in buckets),
        "cache_read_tokens": sum(int(b.get("cache_read_tokens") or 0) for b in buckets),
        "cache_creation_tokens": sum(
            int(b.get("cache_creation_tokens") or 0) for b in buckets
        ),
    }
    costs = [b.get("estimated_cost_usd") for b in buckets]
    total_cost = None if any(c is None for c in costs) else sum(float(c or 0) for c in costs)
    db.upsert_session(
        session_id,
        agent_type,
        provider=provider,
        data_root=data_root,
        project_path=project_path,
        model=model,
        started_at=f"{buckets[0]['usage_date']}T{buckets[0]['usage_hour']:02d}:10:00+08:00",
        ended_at=f"{buckets[-1]['usage_date']}T{buckets[-1]['usage_hour']:02d}:55:00+08:00",
        first_prompt="Summarize demo usage",
        last_prompt="Refresh dashboard",
        estimated_cost_usd=total_cost,
        **total,
    )
    db.replace_session_usage(
        session_id,
        agent_type,
        [
            {
                **bucket,
                "project_path": project_path,
                "model": model,
            }
            for bucket in buckets
        ],
        provider=provider,
        data_root=data_root,
    )


def _bucket(date: str, hour: int, cost: float) -> dict:
    """Build one usage bucket, deriving plausible tokens/messages from cost.

    Keeps the demo internally consistent: tokens, message/turn counts and the
    ~77% cache-hit rate all scale with the dollar cost so every panel reads
    like real, coherent data instead of hand-tuned noise.
    """
    return {
        "usage_date": date,
        "usage_hour": hour,
        "message_count": int(cost * 8) + 6,
        "user_turns": int(cost * 3) + 2,
        "input_tokens": int(cost * 12_000),
        "output_tokens": int(cost * 4_500),
        "cache_read_tokens": int(cost * 52_000),
        "cache_creation_tokens": int(cost * 3_500),
        "estimated_cost_usd": round(cost, 2),
    }


# Demo sessions across three machines (local + two SSH remotes), several
# agents, providers and models. Today (2026-06-14) is dense across the working
# hours so the heatmap, breakdown and summary cards all light up; earlier days
# (and a little May history) feed the trend chart and week/month deltas.
_DEMO_SESSIONS = [
    # ── local ────────────────────────────────────────────────────────
    {
        "session_id": "local-codex-webapp",
        "agent_type": "codex", "provider": "openai",
        "data_root": "/demo/.codex", "project_path": "/workspace/web-app",
        "model": "gpt-5.3-codex",
        "today": [(8, 0.42), (9, 0.88), (11, 1.20), (14, 1.95), (15, 2.40)],
        "history": {"2026-06-13": 2.10, "2026-06-12": 1.60, "2026-06-11": 1.90,
                    "2026-06-10": 1.20, "2026-06-09": 0.90, "2026-06-08": 1.40,
                    "2026-06-07": 0.70, "2026-06-05": 1.10, "2026-06-03": 0.80,
                    "2026-06-01": 0.60, "2026-05-26": 1.20, "2026-05-27": 1.50,
                    "2026-05-28": 1.30, "2026-05-29": 0.90, "2026-05-30": 1.10},
    },
    {
        "session_id": "local-claude-webapp",
        "agent_type": "claude_code", "provider": "anthropic",
        "data_root": "/demo/.claude", "project_path": "/workspace/web-app",
        "model": "claude-opus-4-8",
        "today": [(10, 0.75), (13, 1.10), (16, 0.95)],
        "history": {"2026-06-13": 1.30, "2026-06-12": 0.90, "2026-06-10": 1.10,
                    "2026-06-08": 0.80, "2026-06-05": 0.60, "2026-06-02": 0.50,
                    "2026-05-28": 1.00, "2026-05-29": 0.90, "2026-05-31": 1.20},
    },
    {
        "session_id": "local-claude-api",
        "agent_type": "claude_code", "provider": "anthropic",
        "data_root": "/demo/.claude", "project_path": "/workspace/api-server",
        "model": "claude-sonnet-4-6",
        "today": [(9, 0.30), (12, 0.45), (15, 0.80)],
        "history": {"2026-06-13": 0.60, "2026-06-11": 0.50, "2026-06-09": 0.40,
                    "2026-06-06": 0.30},
    },
    # ── orion (remote) ───────────────────────────────────────────────
    {
        "session_id": "orion-codex-ml",
        "agent_type": "codex", "provider": "openai",
        "data_root": "ssh://orion/home/dev/.codex", "project_path": "/workspace/ml-pipeline",
        "model": "gpt-5.3-codex",
        "today": [(10, 0.60), (14, 1.35), (16, 0.70)],
        "history": {"2026-06-13": 1.00, "2026-06-12": 0.80, "2026-06-09": 0.60},
    },
    {
        "session_id": "orion-claude-ml",
        "agent_type": "claude_code", "provider": "anthropic",
        "data_root": "ssh://orion/home/dev/.claude", "project_path": "/workspace/ml-pipeline",
        "model": "claude-haiku-4-5",
        "today": [(13, 0.25), (15, 0.40)],
        "history": {"2026-06-12": 0.30, "2026-06-10": 0.20},
    },
    # ── nebula (remote, Bedrock billing channel) ─────────────────────
    {
        "session_id": "nebula-claude-infra",
        "agent_type": "claude_code", "provider": "bedrock",
        "data_root": "ssh://nebula/home/dev/.claude", "project_path": "/workspace/infra",
        "model": "claude-sonnet-4-6",
        "today": [(11, 0.55), (14, 0.90)],
        "history": {"2026-06-13": 0.70, "2026-06-11": 0.60},
    },
]


def _seed_demo_db(db_path: Path) -> Database:
    db = Database(db_path=str(db_path))
    for spec in _DEMO_SESSIONS:
        buckets = [_bucket("2026-06-14", hour, cost) for hour, cost in spec["today"]]
        buckets += [
            _bucket(date, 10, cost) for date, cost in sorted(spec["history"].items())
        ]
        buckets.sort(key=lambda b: (b["usage_date"], b["usage_hour"]))
        _add_session(
            db,
            session_id=spec["session_id"],
            agent_type=spec["agent_type"],
            provider=spec["provider"],
            data_root=spec["data_root"],
            project_path=spec["project_path"],
            model=spec["model"],
            buckets=buckets,
        )
    db.commit()
    return db


# Window chrome + cell metrics for the painted screenshot.
_TERM_BG = (39, 40, 34)  # Monokai editor background (#272822), matches the TUI.
_DEFAULT_FG = (213, 213, 213)
_TITLEBAR_BG = (32, 33, 28)
_DOTS = ((255, 95, 86), (255, 189, 46), (39, 201, 63))  # macOS traffic lights
_FONT_PT = 28  # final cell font size; we render at 2x then downsample for crisp glyphs
_SUPERSAMPLE = 2


def _load_demo_font(ImageFont, size: int):
    for path in (
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Monaco.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _seg_rgb(color, default: tuple[int, int, int]) -> tuple[int, int, int]:
    """Resolve a rich Color to an RGB triplet, falling back to ``default``."""
    if color is None:
        return default
    try:
        triplet = color.get_truecolor(theme=MONOKAI)
    except Exception:  # pragma: no cover - defensive
        return default
    return (triplet.red, triplet.green, triplet.blue)


def _capture_screen_lines(app: AgenticMetricApp):
    """Render the composited Textual screen into a grid of styled segments."""
    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
        safe_box=False,
    )
    screen_render = app.screen._compositor.render_update(
        full=True,
        screen_stack=app._background_screens,
        simplify=False,
    )
    assert screen_render is not None
    options = console.options.update(width=width, height=height)
    return console.render_lines(screen_render, options)


def _lines_to_text(lines) -> str:
    return "\n".join(
        "".join(seg.text for seg in line if not seg.text.startswith("\x1b"))
        for line in lines
    )


def _paint_lines_to_png(lines, png_path: Path) -> None:
    """Paint the styled cell grid as a faithful terminal screenshot."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow required for PNG output")
    ImageDraw = pytest.importorskip("PIL.ImageDraw", reason="Pillow required for PNG output")
    ImageFont = pytest.importorskip("PIL.ImageFont", reason="Pillow required for PNG output")

    scale = _SUPERSAMPLE
    font = _load_demo_font(ImageFont, _FONT_PT * scale)
    cell_w = round(font.getlength("M"))
    ascent, descent = font.getmetrics()
    cell_h = ascent + descent

    cols = max((sum(cell_len(s.text) for s in line if not s.text.startswith("\x1b"))
                for line in lines), default=0)
    rows = len(lines)

    pad_x = cell_w
    pad_bottom = round(cell_h * 0.6)
    bar_h = round(cell_h * 1.9)
    content_w = cols * cell_w
    content_h = rows * cell_h
    img_w = content_w + pad_x * 2
    img_h = bar_h + content_h + pad_bottom

    image = Image.new("RGB", (img_w, img_h), _TERM_BG)
    draw = ImageDraw.Draw(image)

    radius = round(cell_h * 0.6)
    draw.rounded_rectangle((0, 0, img_w - 1, img_h - 1), radius=radius, fill=_TERM_BG)
    # Title bar with macOS traffic-light dots.
    draw.rounded_rectangle((0, 0, img_w - 1, bar_h + radius), radius=radius, fill=_TITLEBAR_BG)
    draw.rectangle((0, radius, img_w - 1, bar_h), fill=_TITLEBAR_BG)
    dot_r = round(cell_h * 0.3)
    dot_y = bar_h // 2
    dot_x = pad_x + dot_r
    for color in _DOTS:
        draw.ellipse(
            (dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r), fill=color
        )
        dot_x += round(dot_r * 3.2)

    y = bar_h
    for line in lines:
        x = pad_x
        for seg in line:
            if seg.text.startswith("\x1b"):
                continue
            style = seg.style
            fg = _seg_rgb(style.color if style else None, _DEFAULT_FG)
            bg = _seg_rgb(style.bgcolor if style else None, _TERM_BG)
            for ch in seg.text:
                advance = (cell_len(ch) or 1) * cell_w
                if bg != _TERM_BG:
                    draw.rectangle((x, y, x + advance - 1, y + cell_h - 1), fill=bg)
                if ch == "⭘":
                    # Header command-palette icon: Menlo has no glyph for it, so
                    # draw the heavy-circle ring it represents (matches the look a
                    # symbol-capable terminal font would render).
                    r = round(cell_h * 0.28)
                    cx, cy = x + advance // 2, y + cell_h // 2
                    draw.ellipse(
                        (cx - r, cy - r, cx + r, cy + r), outline=fg, width=scale
                    )
                elif ch != " ":
                    draw.text((x, y), ch, font=font, fill=fg)
                x += advance
        y += cell_h

    # Subtle outer border, then downsample for anti-aliased glyphs.
    draw.rounded_rectangle(
        (0, 0, img_w - 1, img_h - 1), radius=radius, outline=(70, 70, 64), width=scale
    )
    if scale != 1:
        image = image.resize((img_w // scale, img_h // scale), Image.LANCZOS)
    image.save(png_path)


async def _render_demo_png(tmp_path: Path) -> Path:
    db = _seed_demo_db(tmp_path / "demo.db")
    app = AgenticMetricApp(
        db=db,
        collectors=DemoCollectors(),
        sync_on_mount=False,
        show_clock=False,
    )
    app.theme = "monokai"
    app.ansi_theme_dark = MONOKAI
    try:
        async with app.run_test(size=(132, 58), notifications=False) as pilot:
            await pilot.pause()
            lines = _capture_screen_lines(app)
    finally:
        db.close()

    text = _lines_to_text(lines)
    assert "/workspace/web-app" in text
    assert "orion" in text  # remote host (SSH) appears in the breakdown
    assert "codex" in text
    assert "openai" in text
    assert "claude_code" in text
    assert "anthropic" in text

    home = str(Path.home())
    user = os.environ.get("USER") or ""
    assert home not in text
    if user:
        assert user not in text

    png_path = tmp_path / "agentic-metric-screenshot.png"
    _paint_lines_to_png(lines, png_path)
    assert png_path.exists()
    assert png_path.stat().st_size > 25_000
    return png_path


def test_tui_demo_screenshot_png_is_reproducible(monkeypatch, tmp_path):
    """Generate the anonymized TUI demo PNG used by the README screenshot.

    Set ``UPDATE_TUI_DEMO_PNG=1`` to refresh ``agentic-metric-screenshot.png``.
    """
    monkeypatch.setattr("agentic_metric.store.aggregator.datetime", DemoDateTime)
    png_path = asyncio.run(_render_demo_png(tmp_path))

    if os.environ.get("UPDATE_TUI_DEMO_PNG") == "1":
        shutil.copyfile(png_path, README_SCREENSHOT)
        assert README_SCREENSHOT.stat().st_size == png_path.stat().st_size
