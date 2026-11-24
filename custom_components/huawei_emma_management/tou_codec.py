"""Dependency-free EMMA/LUNA2000 time-of-use codec shared by every runtime.

This module is the single definition of how a native EMMA TOU schedule
(register 40004, ``emma_tou_periods``) is represented as JSON, as Huawei LUNA
period text and as editor/service input. The Home Assistant integration, the
embedded and standalone Python connector, the mock client and the ESPHome
firmware tests all use it, so the representation cannot drift between them.

Weekday conventions
-------------------
Huawei encodes the effective weekdays of a period as a bitmask where bit 0 is
Sunday and bit 6 is Saturday. ``huawei-solar`` exposes that bitmask as the
``days_effective`` tuple in the same order (index 0 = Sunday).

People, the LUNA text format and the Home Assistant editor use Monday first:
LUNA day ``1`` is Monday and ``7`` is Sunday (``huawei_solar`` maps a digit
with ``int(day) % 7``). The ``days`` list in this module is therefore Monday
first, while ``days_effective`` always keeps the native Sunday-first order.

Canonical period JSON contains both keys so that consumers of either the
historic Python representation (``charge_flag``/``days_effective``) or the
historic ESPHome representation (``action``/``days``) keep working::

    {"start_time": 0, "end_time": 239, "charge_flag": "charge",
     "action": "charge", "days_effective": [...Sun..Sat],
     "days": [...Mon..Sun]}

Inert periods
-------------
BESS Manager clears a Huawei schedule by writing one period that is effective
on no weekday (``00:00-00:14//+``), because the stock ``huawei_solar`` service
cannot write an empty list. Such a period can never run. This codec accepts it
and normalizes it away, so the EMMA receives a genuinely empty schedule. An
empty string remains the native clear command.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

TOU_MAX_PERIODS = 14
MINUTES_PER_DAY = 1440
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

CHARGE = "charge"
DISCHARGE = "discharge"

_CHARGE_WORDS = {"c": CHARGE, "charge": CHARGE, "0": CHARGE, "+": CHARGE}
_DISCHARGE_WORDS = {"d": DISCHARGE, "discharge": DISCHARGE, "1": DISCHARGE, "-": DISCHARGE}

_LUNA_TOU_LINE = re.compile(
    r"^(?P<start>\d{2}:\d{2})-(?P<end>\d{2}:\d{2})/"
    r"(?P<days>[1-7]*)/(?P<flag>[+-])$"
)


class TouValidationError(ValueError):
    """A TOU schedule is malformed or cannot be represented on the EMMA."""


def days_to_days_effective(days: Iterable[bool]) -> list[bool]:
    """Convert Monday-first weekday booleans to Huawei's Sunday-first order."""
    values = list(days)
    return [values[6], *values[:6]]


def days_effective_to_days(days_effective: Iterable[bool]) -> list[bool]:
    """Convert Huawei's Sunday-first weekday booleans to Monday-first order."""
    values = list(days_effective)
    return [*values[1:7], values[0]]


def days_effective_to_mask(days_effective: Iterable[bool]) -> int:
    """Return the native bitmask (bit 0 = Sunday)."""
    mask = 0
    for bit, enabled in enumerate(days_effective):
        if enabled:
            mask |= 1 << bit
    return mask


def mask_to_days_effective(mask: int) -> list[bool]:
    """Return Sunday-first booleans for a native weekday bitmask."""
    return [bool(mask & (1 << bit)) for bit in range(7)]


def parse_time_minutes(value: Any, field: str = "time") -> int:
    """Parse minutes, ``HH:MM`` or ``HH:MM:SS`` into minutes since midnight."""
    if isinstance(value, bool):
        raise TouValidationError(f"{field} must be minutes, HH:MM, or HH:MM:SS")
    if isinstance(value, str) and ":" in value:
        try:
            parts = value.split(":")
            if len(parts) not in (2, 3):
                raise ValueError
            hour, minute = int(parts[0]), int(parts[1])
            second = int(parts[2]) if len(parts) == 3 else 0
        except ValueError as error:
            raise TouValidationError(
                f"{field} must be minutes, HH:MM, or HH:MM:SS"
            ) from error
        if (
            not 0 <= hour <= 24
            or not 0 <= minute < 60
            or not 0 <= second < 60
            or (hour == 24 and (minute or second))
        ):
            raise TouValidationError(f"{field} must be minutes, HH:MM, or HH:MM:SS")
        return hour * 60 + minute
    try:
        minutes = int(value)
    except (TypeError, ValueError) as error:
        raise TouValidationError(
            f"{field} must be minutes, HH:MM, or HH:MM:SS"
        ) from error
    if isinstance(value, float) and value != minutes:
        raise TouValidationError(f"{field} must be a whole number of minutes")
    return minutes


def format_time_minutes(value: Any) -> str:
    """Format a minute offset as ``HH:MM`` (clamped to 00:00-24:00)."""
    try:
        minutes = max(0, min(MINUTES_PER_DAY, int(value)))
    except (TypeError, ValueError):
        return "--:--"
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}"


def parse_action(value: Any, label: str = "period") -> str:
    """Normalize a charge/discharge flag from any accepted representation."""
    if isinstance(value, bool) or value is None:
        raise TouValidationError(f"{label} action must be charge or discharge")
    if isinstance(value, int):
        value = str(value)
    normalized = str(value).strip().lower()
    if normalized in _CHARGE_WORDS:
        return CHARGE
    if normalized in _DISCHARGE_WORDS:
        return DISCHARGE
    raise TouValidationError(f"{label} action must be charge or discharge")


def _bool_list(value: Any, label: str, field: str) -> list[bool]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 7
        or any(not isinstance(day, bool) for day in value)
    ):
        raise TouValidationError(f"{label} {field} must contain seven booleans")
    return list(value)


def canonical_period(
    start_time: int, end_time: int, action: str, days_effective: Iterable[bool]
) -> dict[str, Any]:
    """Return the canonical JSON representation of one TOU period."""
    native = [bool(day) for day in days_effective]
    return {
        "start_time": int(start_time),
        "end_time": int(end_time),
        "charge_flag": action,
        "action": action,
        "days_effective": native,
        "days": days_effective_to_days(native),
    }


def normalize_period(period: Any, index: int = 1) -> dict[str, Any] | None:
    """Validate one input period and return it in canonical form.

    ``days`` is Monday first, ``days_effective`` is native Sunday first. When
    both are supplied they must describe the same weekdays. Returns ``None``
    for an inert period (effective on no weekday), which can never run.
    """
    label = f"TOU period {index}"
    if not isinstance(period, dict):
        raise TouValidationError(f"{label} must be an object")
    start = parse_time_minutes(period.get("start_time"), f"{label} start_time")
    end = parse_time_minutes(period.get("end_time"), f"{label} end_time")
    if not 0 <= start < end <= MINUTES_PER_DAY:
        raise TouValidationError(f"{label} must have 0 <= start < end <= 1440")
    action = parse_action(
        period.get("action", period.get("charge_flag", period.get("mode"))), label
    )
    native: list[bool] | None = None
    if "days_effective" in period and period["days_effective"] is not None:
        native = _bool_list(period["days_effective"], label, "days_effective")
    if "days" in period and period["days"] is not None:
        from_days = days_to_days_effective(_bool_list(period["days"], label, "days"))
        if native is not None and native != from_days:
            raise TouValidationError(
                f"{label} days (Mon-Sun) and days_effective (Sun-Sat) disagree"
            )
        native = from_days
    if native is None:
        native = [True] * 7
    if not any(native):
        return None
    return canonical_period(start, end, action, native)


def normalize_periods(periods: Any) -> list[dict[str, Any]]:
    """Validate a complete schedule, dropping inert periods.

    Raises ``TouValidationError`` for more than 14 periods, malformed periods,
    or periods that overlap on an enabled weekday.
    """
    if not isinstance(periods, (list, tuple)):
        raise TouValidationError(
            f"TOU periods must be a list containing 0 to {TOU_MAX_PERIODS} periods"
        )
    if len(periods) > TOU_MAX_PERIODS:
        raise TouValidationError(
            f"TOU schedule accepts at most {TOU_MAX_PERIODS} periods"
        )
    result = [
        normalized
        for index, period in enumerate(periods, start=1)
        if (normalized := normalize_period(period, index)) is not None
    ]
    validate_no_overlap(result)
    return result


def validate_no_overlap(periods: list[dict[str, Any]]) -> None:
    """Reject canonical periods that overlap on the same weekday."""
    for day in range(7):
        active = sorted(
            (period for period in periods if period["days_effective"][day]),
            key=lambda item: item["start_time"],
        )
        for previous, current in zip(active, active[1:]):
            if current["start_time"] < previous["end_time"]:
                raise TouValidationError(
                    "TOU periods overlap on "
                    f"{WEEKDAY_NAMES[(day - 1) % 7]}: "
                    f"{format_time_minutes(previous['start_time'])}-"
                    f"{format_time_minutes(previous['end_time'])} and "
                    f"{format_time_minutes(current['start_time'])}-"
                    f"{format_time_minutes(current['end_time'])}"
                )


def canonical_readback(periods: Any) -> list[dict[str, Any]] | None:
    """Normalize a decoded register value without dropping or rejecting periods.

    Accepts huawei-solar dataclass JSON (``charge_flag``/``days_effective``),
    canonical JSON, and the JSON of ESPHome firmware older than 0.15.0, which
    published only ``action``/``days`` with ``days`` in native register bit
    order (bit 0 = Sunday). A readback without ``days_effective`` is therefore
    interpreted as native order. Returns ``None`` when the value is not a
    list, so an unavailable schedule is never reported as an empty one.
    """
    if not isinstance(periods, list):
        return None
    result: list[dict[str, Any]] = []
    for period in periods:
        if not isinstance(period, dict):
            return None
        try:
            action = parse_action(period.get("charge_flag", period.get("action")))
            start = int(period["start_time"])
            end = int(period["end_time"])
        except (KeyError, TypeError, ValueError):
            return None
        if isinstance(period.get("days_effective"), (list, tuple)):
            native = [bool(day) for day in period["days_effective"]]
        elif isinstance(period.get("days"), (list, tuple)):
            # Pre-0.15 ESPHome firmware: ``days`` was the raw bit order.
            native = [bool(day) for day in period["days"]]
        else:
            native = [True] * 7
        if len(native) != 7:
            return None
        result.append(canonical_period(start, end, action, native))
    return result


def schedule_fingerprint(periods: list[dict[str, Any]]) -> tuple[Any, ...]:
    """Return an order-independent identity for schedule comparison."""
    return tuple(
        sorted(
            (
                period["start_time"],
                period["end_time"],
                period["action"],
                tuple(period["days_effective"]),
            )
            for period in periods
        )
    )


def schedules_equal(left: Any, right: Any) -> bool:
    """Compare two schedules after canonical normalization."""
    left_value = canonical_readback(left)
    right_value = canonical_readback(right)
    if left_value is None or right_value is None:
        return False
    return schedule_fingerprint(left_value) == schedule_fingerprint(right_value)


def decode_luna_text(value: Any) -> list[dict[str, Any]]:
    """Decode LUNA text (``HH:MM-HH:MM/DAYS/+|-`` per line) into input periods.

    Returns Monday-first ``days`` periods including inert (no-day) periods, so
    callers can distinguish "clear" from "malformed"; use
    :func:`normalize_periods` to validate and drop inert periods.

    Newlines are the canonical separator. Home Assistant's "Fill example data"
    serializes a multiline example as a space-separated scalar, so a
    whitespace-separated list of otherwise valid periods is also accepted.
    """
    if not isinstance(value, str):
        raise TouValidationError("LUNA TOU periods must be a string")
    if not value.strip():
        return []

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) == 1:
        parts = lines[0].split()
        if len(parts) > 1 and all(_LUNA_TOU_LINE.fullmatch(part) for part in parts):
            lines = parts
    if len(lines) > TOU_MAX_PERIODS:
        raise TouValidationError(
            f"LUNA TOU schedule accepts at most {TOU_MAX_PERIODS} periods"
        )

    periods: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        match = _LUNA_TOU_LINE.fullmatch(line)
        if match is None:
            raise TouValidationError(
                f"LUNA TOU line {index} must use HH:MM-HH:MM/DAYS/+ or -"
            )
        start = parse_time_minutes(match.group("start"), f"line {index} start")
        end = parse_time_minutes(match.group("end"), f"line {index} end")
        if not 0 <= start < end <= MINUTES_PER_DAY:
            raise TouValidationError(f"LUNA TOU line {index} must have start before end")
        day_text = match.group("days")
        if len(set(day_text)) != len(day_text):
            raise TouValidationError(
                f"LUNA TOU line {index} days must be unique digits from 1 to 7"
            )
        selected = set(day_text)
        periods.append(
            {
                "start_time": start,
                "end_time": end,
                "action": CHARGE if match.group("flag") == "+" else DISCHARGE,
                "days": [str(day) in selected for day in range(1, 8)],
            }
        )
    return periods


def format_luna_period(period: dict[str, Any]) -> str | None:
    """Format one canonical period as LUNA text, or ``None`` if inert."""
    days = period.get("days")
    if not isinstance(days, list):
        days = days_effective_to_days(period["days_effective"])
    day_text = "".join(str(number) for number, enabled in enumerate(days, 1) if enabled)
    if not day_text:
        return None
    flag = "+" if period["action"] == CHARGE else "-"
    return (
        f"{format_time_minutes(period['start_time'])}-"
        f"{format_time_minutes(period['end_time'])}/{day_text}/{flag}"
    )


def encode_luna_text(periods: Any) -> str:
    """Encode a schedule as newline-separated LUNA text."""
    canonical = canonical_readback(periods) or []
    return "\n".join(
        line for period in canonical if (line := format_luna_period(period)) is not None
    )


def numbered_period_attributes(periods: Any) -> dict[str, str]:
    """Return ``{"Period 1": "00:00-03:59/1234567/+", ...}`` like huawei_solar.

    BESS Manager reads Huawei schedules from these numbered attributes
    (``HomeAssistantAPIController.read_huawei_tou_periods``).
    """
    canonical = canonical_readback(periods) or []
    lines = [
        line for period in canonical if (line := format_luna_period(period)) is not None
    ]
    return {f"Period {index}": line for index, line in enumerate(lines, start=1)}
