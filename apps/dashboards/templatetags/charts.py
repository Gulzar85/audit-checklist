"""Server-rendered SVG charts (Phase 20) - no chart library, no client script, CSP-safe.

Every chart is an ``<svg role="img">`` with a ``<title>`` and a ``<desc>`` summary; colors come from theme tokens through CSS classes (``chart-s1`` ... in
``tailwind/input.css``), series are also told apart by dash style and labelled in text, value axes start at zero, and every label is escaped.  The template shows the
same numbers in a table next to the chart for screen readers and keyboard users.
"""

from __future__ import annotations

import uuid
from typing import Any

from django import template
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

register = template.Library()

W = 600


def fmt(value: Any, unit: str = "count") -> str:
    if value is None:
        return "-"
    if unit == "percent":
        return f"{float(value):.1f}%"
    if unit == "minutes":
        return f"{float(value):.1f} min"
    return f"{int(value):,}" if float(value).is_integer() else f"{float(value):,.2f}"


register.filter("chart_fmt", fmt)


def _short(text: str, n: int = 26) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1
    for step in (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000):
        if value <= step:
            return step
    return value


def _summary(data: dict[str, Any]) -> str:
    metrics = data.get("metrics") or []
    parts = []
    for row in (data.get("series") or [])[:12]:
        values = ", ".join(f"{m['label']} {fmt(row['values'].get(m['key']), m['unit'])}" for m in metrics)
        parts.append(f"{row['label']}: {values}")
    return "; ".join(parts) or "No data."


def _frame(data: dict[str, Any], height: int, body: str, width: int = W) -> str:
    ident = f"c{uuid.uuid4().hex[:8]}"
    return format_html('<svg class="chart" viewBox="0 0 {} {}" role="img" aria-labelledby="{}-t {}-d" preserveAspectRatio="xMidYMid meet">'
                       '<title id="{}-t">{}</title><desc id="{}-d">{}</desc>{}</svg>', width, height, ident, ident, ident, data.get("title", ""), ident, _summary(data), mark_safe(body))  # noqa: S308 - body is built from escaped parts below


@register.simple_tag
def bar_chart(data: dict[str, Any]) -> str:
    """Horizontal bars (one or two metrics per category), zero-based, values written at the end of each bar."""
    series, metrics = data.get("series") or [], data.get("metrics") or []
    if not series or not metrics:
        return ""
    peak = _nice_max(max([float(r["values"].get(m["key"]) or 0) for r in series for m in metrics] + [0]))
    if any(m["unit"] == "percent" for m in metrics):
        peak = max(peak, 100) if peak <= 100 else peak
    label_w, bar_h = 210, 22
    row_h = (bar_h + 6) * len(metrics) + 12
    chart_w = W - label_w - 90
    parts = []
    for i, row in enumerate(series):
        y = 8 + i * row_h
        parts.append(f'<text class="chart-label" x="{label_w - 10}" y="{y + bar_h - 5}" text-anchor="end">{escape(_short(row["label"], 22))}</text>')
        for j, metric in enumerate(metrics):
            value = row["values"].get(metric["key"])
            width = 0 if value is None else max(1.0, chart_w * float(value) / peak) if float(value) > 0 else 0
            by = y + j * (bar_h + 6)
            parts.append(f'<rect class="chart-bar chart-s{j + 1}" x="{label_w}" y="{by}" width="{width:.1f}" height="{bar_h}" rx="2"><title>{escape(row["label"])} - {escape(metric["label"])}: '
                         f'{escape(fmt(value, metric["unit"]))}</title></rect>')
            parts.append(f'<text class="chart-value" x="{label_w + width + 6:.1f}" y="{by + bar_h - 5}">{escape(fmt(value, metric["unit"]))}</text>')
    parts.append(f'<line class="chart-axis" x1="{label_w}" y1="4" x2="{label_w}" y2="{8 + len(series) * row_h}"/>')
    return _frame(data, 12 + len(series) * row_h, "".join(parts))


@register.simple_tag
def line_chart(data: dict[str, Any]) -> str:
    """A zero-based time series; one line per metric (solid / dashed), a point per bucket with a tooltip; empty averages leave a gap instead of dropping to zero."""
    series, metrics = data.get("series") or [], data.get("metrics") or []
    if not series or not metrics:
        return ""
    height, left, right, top, bottom = 300, 70, 30, 16, 46
    peak = _nice_max(max([float(r["values"].get(m["key"]) or 0) for r in series for m in metrics] + [0]))
    plot_w, plot_h = W - left - right, height - top - bottom
    step = plot_w / max(1, len(series) - 1)
    parts = []
    for frac in (0, 0.5, 1):
        y = top + plot_h * (1 - frac)
        parts.append(f'<line class="chart-grid" x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}"/><text class="chart-tick" x="{left - 8}" y="{y + 6:.1f}" text-anchor="end">'
                     f'{escape(fmt(peak * frac, metrics[0]["unit"]))}</text>')
    every = max(1, -(-len(series) // 5))  # at most ~5 labels so they never overlap
    shown = set(range(0, len(series), every))
    last = len(series) - 1
    if last not in shown:  # always label the latest bucket, dropping its neighbour if they would collide
        if last - max(shown) < every * 0.6:
            shown.discard(max(shown))
        shown.add(last)
    for i, row in enumerate(series):
        if i in shown:
            parts.append(f'<text class="chart-tick" x="{left + i * step:.1f}" y="{height - 14}" text-anchor="middle">{escape(_short(row["label"], 10))}</text>')
    for j, metric in enumerate(metrics):
        segments, current = [], []
        for i, row in enumerate(series):
            value = row["values"].get(metric["key"])
            if value is None:
                if current:
                    segments.append(current)
                current = []
                continue
            x, y = left + i * step, top + plot_h * (1 - float(value) / peak)
            current.append((x, y))
            parts.append(f'<circle class="chart-point chart-s{j + 1}" cx="{x:.1f}" cy="{y:.1f}" r="5"><title>{escape(row["label"])} - {escape(metric["label"])}: '
                         f'{escape(fmt(value, metric["unit"]))}</title></circle>')
        if current:
            segments.append(current)
        for seg in segments:
            if len(seg) > 1:
                points = " ".join(f"{x:.1f},{y:.1f}" for x, y in seg)
                parts.insert(0, f'<polyline class="chart-line chart-s{j + 1}" points="{points}"/>')
    parts.append(f'<line class="chart-axis" x1="{left}" y1="{top + plot_h}" x2="{W - right}" y2="{top + plot_h}"/>')
    return _frame(data, height, "".join(parts))


@register.simple_tag
def donut(data: dict[str, Any]) -> str:
    """Up to six categories (more are shown as bars by the template); the legend carries every label, value and share as text."""
    series, metrics = data.get("series") or [], data.get("metrics") or []
    if not series or not metrics:
        return ""
    key = metrics[0]["key"]
    total = sum(float(r["values"].get(key) or 0) for r in series)
    if total <= 0:
        return ""
    radius, circumference, offset, parts = 95, 2 * 3.14159265 * 95, 0.0, []
    for i, row in enumerate(series):
        value = float(row["values"].get(key) or 0)
        length = circumference * value / total
        parts.append(f'<circle class="chart-seg chart-c{i % 6 + 1}" cx="240" cy="130" r="{radius}" stroke-dasharray="{length:.2f} {circumference - length:.2f}" stroke-dashoffset="{-offset:.2f}">'
                     f'<title>{escape(row["label"])}: {escape(fmt(value))} ({value * 100 / total:.0f}%)</title></circle>')
        offset += length
        parts.append(f'<rect class="chart-c{i % 6 + 1} chart-swatch" x="20" y="{262 + i * 44}" width="26" height="26" rx="4"/><text class="chart-label chart-legend" x="58" y="{284 + i * 44}">'
                     f'{escape(_short(row["label"], 18))} - {escape(fmt(value))} ({value * 100 / total:.0f}%)</text>')
    parts.append(f'<text class="chart-total" x="240" y="142" text-anchor="middle">{escape(fmt(total))}</text>')
    return _frame(data, 274 + len(series) * 44, "".join(parts), width=480)
