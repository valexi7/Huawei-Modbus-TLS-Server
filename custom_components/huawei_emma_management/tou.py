from __future__ import annotations

import json
import re
from typing import Any

from .const import (
    GROWATT_BATTERY_MODES,
    GROWATT_MAX_SEGMENTS,
    TOU_MAX_PERIODS,
    TOU_MODES,
    TOU_REGISTER_NAME,
)


def editor_description(
    field: str,
    name: str,
    icon: str,
) -> dict[str, Any]:
    return {
        "register_name": f"tou_{field}",
        "source_register_name": TOU_REGISTER_NAME,
        "name": name,
        "device_role": "emma",
        "entity_category": "config",
        "enabled_default": True,
        "icon": icon,
        "address": 40004,
        "poll_group": "medium",
    }


def format_tou_schedule(periods: Any) -> tuple[str, list[str]]:
    if not isinstance(periods, list) or not periods:
        return "No periods configured", []
    lines: list[str] = []
    for index, period in enumerate(periods, start=1):
        if not isinstance(period, dict):
            continue
        mode = period.get("action", period.get("charge_flag", "unknown"))
        if isinstance(mode, int):
            mode = "discharge" if mode == 1 else "charge"
        lines.append(
            f"{index}: {_format_minutes(period.get('start_time'))} - "
            f"{_format_minutes(period.get('end_time'))} {str(mode).title()}"
        )
    return "\n".join(lines) if lines else "No periods configured", lines


def encode_tou_plan_json(periods: list[dict[str, Any]]) -> str:
    """Encode up to 14 periods into Home Assistant's 255-byte text state."""
    compact = [
        [
            int(period["start_time"]),
            int(period["end_time"]),
            "d"
            if str(period.get("action", period.get("charge_flag"))).lower()
            in ("d", "discharge", "1")
            else "c",
        ]
        for period in periods
    ]
    value = json.dumps(compact, separators=(",", ":"))
    if len(value) > 255:
        raise ValueError("The compact TOU plan exceeds Home Assistant's 255-character limit")
    return value


def decode_tou_plan_json(value: str) -> list[dict[str, Any]]:
    """Decode compact arrays or verbose period objects from an automation."""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid TOU plan JSON: {error.msg}") from error
    if not isinstance(payload, list) or len(payload) > TOU_MAX_PERIODS:
        raise ValueError(f"TOU plan must be a JSON list with 0-{TOU_MAX_PERIODS} periods")

    periods: list[dict[str, Any]] = []
    for index, item in enumerate(payload, start=1):
        if isinstance(item, list) and len(item) == 3:
            start_value, end_value, mode_value = item
            days = [True] * 7
        elif isinstance(item, dict):
            start_value = item.get("start_time", item.get("start", item.get("s")))
            end_value = item.get("end_time", item.get("end", item.get("e")))
            mode_value = item.get(
                "action", item.get("charge_flag", item.get("mode", item.get("m")))
            )
            days = item.get("days", item.get("days_effective", [True] * 7))
        else:
            raise ValueError(
                f"TOU period {index} must be [start_minutes,end_minutes,mode] or an object"
            )

        start = parse_time_minutes(start_value, f"period {index} start")
        end = parse_time_minutes(end_value, f"period {index} end")
        if not 0 <= start < end <= 1440:
            raise ValueError(f"TOU period {index} must have 0 <= start < end <= 1440")
        mode = _parse_mode(mode_value, index)
        if (
            not isinstance(days, list)
            or len(days) != 7
            or any(not isinstance(day, bool) for day in days)
        ):
            raise ValueError(f"TOU period {index} days must contain seven booleans")
        periods.append(
            {
                "start_time": start,
                "end_time": end,
                "action": mode,
                "days": list(days),
            }
        )
    return periods


_LUNA_TOU_LINE = re.compile(
    r"^(?P<start>\d{2}:\d{2})-(?P<end>\d{2}:\d{2})/"
    r"(?P<days>[1-7]+)/(?P<flag>[+-])$"
)


def decode_luna_tou_periods(value: str) -> list[dict[str, Any]]:
    """Decode the newline-separated LUNA schedule accepted by the external API."""
    if not isinstance(value, str):
        raise ValueError("LUNA TOU periods must be a string")
    if not value.strip():
        return []

    lines = value.splitlines()
    if len(lines) > TOU_MAX_PERIODS:
        raise ValueError(f"LUNA TOU schedule accepts at most {TOU_MAX_PERIODS} periods")

    periods: list[dict[str, Any]] = []
    for index, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        match = _LUNA_TOU_LINE.fullmatch(line)
        if match is None:
            raise ValueError(
                f"LUNA TOU line {index} must use HH:MM-HH:MM/DAYS/+ or -"
            )
        start = parse_time_minutes(match.group("start"), f"line {index} start")
        end = parse_time_minutes(match.group("end"), f"line {index} end")
        if not 0 <= start < end <= 1440:
            raise ValueError(f"LUNA TOU line {index} must have start before end")

        day_text = match.group("days")
        if len(set(day_text)) != len(day_text) or day_text != "".join(sorted(day_text)):
            raise ValueError(
                f"LUNA TOU line {index} days must be unique and ordered from 1 to 7"
            )
        selected_days = set(day_text)
        periods.append(
            {
                "start_time": start,
                "end_time": end,
                "action": "charge" if match.group("flag") == "+" else "discharge",
                "days": [str(day) in selected_days for day in range(1, 8)],
            }
        )
    return periods


def encode_luna_tou_periods(periods: Any) -> str:
    """Encode an EMMA TOU readback using the LUNA text service format."""
    if not isinstance(periods, list):
        return ""

    lines: list[str] = []
    for index, period in enumerate(periods, start=1):
        if not isinstance(period, dict):
            continue
        start = parse_time_minutes(period.get("start_time"), f"period {index} start")
        end = parse_time_minutes(period.get("end_time"), f"period {index} end")
        days = period.get("days", period.get("days_effective", [True] * 7))
        if (
            not isinstance(days, list)
            or len(days) != 7
            or any(not isinstance(day, bool) for day in days)
        ):
            raise ValueError(f"TOU period {index} days must contain seven booleans")
        day_text = "".join(
            str(day_number)
            for day_number, enabled in enumerate(days, start=1)
            if enabled
        )
        if not day_text:
            continue
        mode = str(period.get("action", period.get("charge_flag", "discharge"))).lower()
        flag = "+" if mode in ("charge", "c", "0") else "-"
        lines.append(
            f"{format_time_minutes(start)}-{format_time_minutes(end)}/{day_text}/{flag}"
        )
    return "\n".join(lines)


def parse_time_minutes(value: Any, field: str = "time") -> int:
    if isinstance(value, str) and ":" in value:
        try:
            parts = value.split(":")
            if len(parts) not in (2, 3):
                raise ValueError
            hour, minute = int(parts[0]), int(parts[1])
            second = int(parts[2]) if len(parts) == 3 else 0
        except ValueError as error:
            raise ValueError(f"{field} must be minutes, HH:MM, or HH:MM:SS") from error
        if (
            not 0 <= hour <= 24
            or not 0 <= minute < 60
            or not 0 <= second < 60
            or (hour == 24 and (minute or second))
        ):
            raise ValueError(f"{field} must be minutes, HH:MM, or HH:MM:SS")
        return hour * 60 + minute
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be minutes, HH:MM, or HH:MM:SS") from error


def growatt_mode_to_tou_mode(batt_mode: str) -> str:
    """Map Growatt's three battery priorities to EMMA's charge/discharge flag."""
    normalized = str(batt_mode).strip().lower()
    if normalized not in GROWATT_BATTERY_MODES:
        raise ValueError(
            "batt_mode must be load_first, battery_first, or grid_first"
        )
    return "charge" if normalized == "battery_first" else "discharge"


def tou_mode_to_growatt_mode(mode: Any) -> str:
    """Return the closest lossless Growatt mode for an EMMA TOU action."""
    normalized = str(mode).strip().lower()
    return "battery_first" if normalized in ("charge", "c", "0") else "load_first"


def growatt_time_segments(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the response shape used by growatt_server.read_time_segments."""
    response: list[dict[str, Any]] = []
    for index in range(GROWATT_MAX_SEGMENTS):
        slot = slots[index] if index < len(slots) else {}
        response.append(
            {
                "segment_id": index + 1,
                "start_time": format_time_minutes(slot.get("start_time", 0)),
                "end_time": format_time_minutes(slot.get("end_time", 0)),
                "batt_mode": slot.get("batt_mode")
                or tou_mode_to_growatt_mode(slot.get("mode", "discharge")),
                "enabled": bool(slot.get("enabled", False)),
            }
        )
    return response


def _parse_mode(value: Any, index: int) -> str:
    if isinstance(value, bool):
        raise ValueError(f"TOU period {index} mode must be c/charge or d/discharge")
    normalized = str(value).strip().lower()
    mode = {
        "c": "charge",
        "charge": "charge",
        "0": "charge",
        "d": "discharge",
        "discharge": "discharge",
        "1": "discharge",
    }.get(normalized)
    if mode not in TOU_MODES:
        raise ValueError(f"TOU period {index} mode must be c/charge or d/discharge")
    return mode


def _format_minutes(value: Any) -> str:
    try:
        minutes = max(0, min(1440, int(value)))
    except (TypeError, ValueError):
        return "--:--"
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}"


def format_time_minutes(value: Any) -> str:
    """Format a minute offset as the HH:MM value used by Home Assistant actions."""
    return _format_minutes(value)
