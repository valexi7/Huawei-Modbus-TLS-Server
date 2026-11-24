#!/usr/bin/env python3
"""Dump the installed huawei-solar register catalog as canonical JSON.

Run this with two different huawei-solar versions installed and compare the
outputs with ``tools/register_catalog_diff.py`` to produce a migration report.
"""

from __future__ import annotations

import enum
import functools
import json
import sys
from importlib.metadata import version
from typing import Any

from huawei_solar.registers import REGISTERS


def _unit(unit: Any) -> Any:
    if unit is None or isinstance(unit, (str, int, float)):
        return unit
    if unit is bool:
        return {"kind": "bool"}
    if isinstance(unit, type) and issubclass(unit, enum.Enum):
        kind = "intflag" if issubclass(unit, enum.IntFlag) else "enum"
        return {
            "kind": kind,
            "name": unit.__name__,
            "members": {member.name: int(member.value) for member in unit},
        }
    if isinstance(unit, dict):
        return {"kind": "map", "members": {str(k): str(v) for k, v in unit.items()}}
    if isinstance(unit, functools.partial):
        mapping = unit.args[0] if unit.args else {}
        members: dict[str, str] = {}
        if isinstance(mapping, dict):
            for mask, value in mapping.items():
                members[str(mask)] = str(getattr(value, "on_value", getattr(value, "name", value)))
        return {"kind": "bitfield", "function": getattr(unit.func, "__name__", "?"), "members": members}
    return {"kind": type(unit).__name__}


def _targets(register: Any) -> list[str]:
    target = register.target_device
    return sorted(member.name for member in type(target) if member in target and member.value)


def dump() -> dict[str, Any]:
    registers: dict[str, Any] = {}
    for name, register in REGISTERS.items():
        invalid = getattr(register, "invalid_value", None)
        registers[str(name)] = {
            "address": register.register,
            "length": register.length,
            "type": type(register).__name__,
            "gain": getattr(register, "gain", None),
            "unit": _unit(getattr(register, "unit", None)),
            "readable": bool(register.readable),
            "writeable": bool(register.writeable),
            "targets": _targets(register),
            "invalid_value": invalid if invalid is None or isinstance(invalid, (int, float)) else str(invalid),
        }
    return {"huawei_solar": version("huawei-solar"), "registers": registers}


if __name__ == "__main__":
    json.dump(dump(), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
