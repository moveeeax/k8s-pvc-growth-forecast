"""Table, JSON and Markdown rendering of the forecast."""

from __future__ import annotations

import json
from typing import List, Optional

from .fit import (
    STATUS_CRITICAL,
    STATUS_LOW_CONFIDENCE,
    STATUS_NO_DATA,
    STATUS_OK,
    STATUS_STABLE,
    STATUS_WARNING,
    Forecast,
)

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(value: Optional[float]) -> str:
    """Binary-prefix formatting, because that is what kubelet reports and kubectl prints."""
    if value is None:
        return "-"
    negative = value < 0
    size = abs(float(value))
    index = 0
    # 1023.95 rather than 1024: a slope of 1 GiB/day lands a hair under 2**30 in floating
    # point, and printing it as "1024.0MiB/d" instead of "1.0GiB/d" looks like a bug.
    while size >= 1023.95 and index < len(_UNITS) - 1:
        size /= 1024.0
        index += 1
    text = "{:.0f}{}".format(size, _UNITS[index]) if index == 0 else "{:.1f}{}".format(size, _UNITS[index])
    return "-" + text if negative else text


def human_days(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value >= 1000:
        return ">1000"
    if value >= 10:
        return "{:.0f}".format(value)
    return "{:.1f}".format(value)


def _rate(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if abs(value) < 1024:
        return "{:+.0f}B/d".format(value)
    return "{}{}/d".format("+" if value > 0 else "-", human_bytes(abs(value)))


def _percent(value: Optional[float]) -> str:
    return "-" if value is None else "{:.0f}%".format(value)


def render_table(forecasts: List[Forecast]) -> str:
    if not forecasts:
        return "no PersistentVolumeClaims matched the query"
    header = ["STATUS", "NAMESPACE", "CLAIM", "USED", "CAPACITY", "USED%", "GROWTH", "DAYS", "R2"]
    rows = []
    for item in forecasts:
        rows.append(
            [
                item.status,
                item.key.namespace,
                item.key.claim,
                human_bytes(item.used_bytes),
                human_bytes(item.capacity_bytes),
                _percent(item.used_percent),
                _rate(item.growth_bytes_per_day),
                human_days(item.days_to_full if item.status != STATUS_STABLE else None),
                "-" if item.r2 is None else "{:.2f}".format(item.r2),
            ]
        )
    widths = [max(len(row[i]) for row in [header] + rows) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)).rstrip()]
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    notes = [(item, item.note) for item in forecasts if item.note]
    if notes:
        lines.append("")
        for item, note in notes:
            lines.append("note  {}: {}".format(item.key, note))
    return "\n".join(lines)


def render_json(forecasts: List[Forecast]) -> str:
    payload = {
        "summary": {
            status: sum(1 for item in forecasts if item.status == status)
            for status in (
                STATUS_CRITICAL,
                STATUS_WARNING,
                STATUS_OK,
                STATUS_STABLE,
                STATUS_LOW_CONFIDENCE,
                STATUS_NO_DATA,
            )
        },
        "volumes": [
            {
                "namespace": item.key.namespace,
                "persistentvolumeclaim": item.key.claim,
                "status": item.status,
                "used_bytes": _round(item.used_bytes),
                "capacity_bytes": _round(item.capacity_bytes),
                "free_bytes": _round(item.free_bytes),
                "used_percent": _round(item.used_percent, 2),
                "growth_bytes_per_day": _round(item.growth_bytes_per_day),
                "days_to_full": _round(item.days_to_full, 2),
                "r2": _round(item.r2, 4),
                "samples": item.samples,
                "resized_at": _round(item.resized_at),
                "note": item.note,
            }
            for item in forecasts
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def _round(value: Optional[float], digits: int = 0):
    if value is None:
        return None
    return round(float(value), digits) if digits else round(float(value))


_ICONS = {
    STATUS_CRITICAL: "🔴",
    STATUS_WARNING: "🟠",
    STATUS_OK: "🟢",
    STATUS_STABLE: "⚪",
    STATUS_LOW_CONFIDENCE: "🔵",
    STATUS_NO_DATA: "⚫",
}


def render_markdown(forecasts: List[Forecast]) -> str:
    if not forecasts:
        return "**PVC growth forecast:** no PersistentVolumeClaims matched the query."
    counts = {}
    for item in forecasts:
        counts[item.status] = counts.get(item.status, 0) + 1
    summary = ", ".join(
        "{} {}".format(counts[status], status)
        for status in (
            STATUS_CRITICAL,
            STATUS_WARNING,
            STATUS_OK,
            STATUS_LOW_CONFIDENCE,
            STATUS_STABLE,
            STATUS_NO_DATA,
        )
        if counts.get(status)
    )
    lines = [
        "### PVC growth forecast",
        "",
        "{} volume(s): {}".format(len(forecasts), summary),
        "",
        "| | Namespace | Claim | Used | Capacity | Growth | Days to full | R² |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in forecasts:
        lines.append(
            "| {} | `{}` | `{}` | {} ({}) | {} | {} | {} | {} |".format(
                _ICONS.get(item.status, ""),
                item.key.namespace,
                item.key.claim,
                human_bytes(item.used_bytes),
                _percent(item.used_percent),
                human_bytes(item.capacity_bytes),
                _rate(item.growth_bytes_per_day),
                human_days(item.days_to_full if item.status != STATUS_STABLE else None),
                "-" if item.r2 is None else "{:.2f}".format(item.r2),
            )
        )
    notes = [item for item in forecasts if item.note]
    if notes:
        lines.append("")
        for item in notes:
            lines.append("- `{}`: {}".format(item.key, item.note))
    return "\n".join(lines)


def render(forecasts: List[Forecast], fmt: str) -> str:
    if fmt == "json":
        return render_json(forecasts)
    if fmt == "markdown":
        return render_markdown(forecasts)
    return render_table(forecasts)
