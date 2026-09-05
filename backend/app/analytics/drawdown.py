"""Running peak and drawdown calculations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from ._common import dated_records, decimal, field


@dataclass(frozen=True, slots=True)
class DrawdownPoint:
    valuation_date: date
    value: Decimal | None
    peak_value: Decimal | None
    drawdown: Decimal | None
    max_drawdown: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DrawdownResult:
    points: tuple[DrawdownPoint, ...]
    current_drawdown: Decimal | None
    max_drawdown: Decimal | None
    peak_date: date | None
    trough_date: date | None
    peak_value: Decimal | None
    trough_value: Decimal | None


def calculate_drawdown(records: list[Any] | tuple[Any, ...]) -> DrawdownResult:
    """Calculate drawdown from NAV points without filling missing values."""

    dated = dated_records(records)
    running_peak: Decimal | None = None
    running_peak_date: date | None = None
    worst: Decimal | None = None
    worst_peak_date: date | None = None
    worst_trough_date: date | None = None
    worst_peak_value: Decimal | None = None
    worst_trough_value: Decimal | None = None
    points: list[DrawdownPoint] = []

    for valuation_day, record in dated:
        value = decimal(field(record, "adjusted_nav", "cumulative_unit_nav", "value"))
        if value is None:
            points.append(DrawdownPoint(valuation_day, None, running_peak, None, worst))
            continue

        if running_peak is None or value > running_peak:
            running_peak = value
            running_peak_date = valuation_day

        drawdown = None if running_peak == 0 else value / running_peak - Decimal(1)
        if drawdown is not None and (worst is None or drawdown < worst):
            worst = drawdown
            worst_peak_date = running_peak_date
            worst_trough_date = valuation_day
            worst_peak_value = running_peak
            worst_trough_value = value
        points.append(
            DrawdownPoint(valuation_day, value, running_peak, drawdown, worst)
        )

    current_drawdown = points[-1].drawdown if points else None
    if worst is None and running_peak not in (None, Decimal(0)):
        worst = Decimal(0)
        worst_peak_date = next(
            point.valuation_date for point in points if point.value is not None
        )
        worst_trough_date = worst_peak_date
        worst_peak_value = running_peak
        worst_trough_value = running_peak
    return DrawdownResult(
        points=tuple(points),
        current_drawdown=current_drawdown,
        max_drawdown=worst,
        peak_date=worst_peak_date,
        trough_date=worst_trough_date,
        peak_value=worst_peak_value,
        trough_value=worst_trough_value,
    )


def calculate_max_drawdown(records: list[Any] | tuple[Any, ...]) -> DrawdownResult:
    return calculate_drawdown(records)
