#!/usr/bin/env python3
"""Write a Markdown migration report between two register/entity catalogs.

Inputs:

* two ``tools/dump_register_catalog.py`` outputs (old and new huawei-solar);
* the 0.14.9 entity baseline shipped with the integration, compared with the
  entity catalog built from the currently installed huawei-solar.

Example::

    venv-old/bin/python tools/dump_register_catalog.py > old.json
    venv-new/bin/python tools/dump_register_catalog.py > new.json
    venv-new/bin/python tools/register_catalog_diff.py old.json new.json \
        > docs/register-catalog-0.15.0.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REGISTER_FIELDS = ("address", "length", "type", "gain", "unit", "writeable", "targets", "invalid_value")


def _fmt(value: Any) -> str:
    if isinstance(value, dict):
        kind = value.get("kind")
        if kind in ("enum", "intflag"):
            return f"{kind} {value.get('name')} ({len(value.get('members', {}))} members)"
        if kind in ("map", "bitfield"):
            return f"{kind} ({len(value.get('members', {}))} members)"
        return str(kind)
    if isinstance(value, list):
        return ", ".join(value) or "-"
    return "-" if value is None else str(value)


def _members_change(old: Any, new: Any) -> str:
    if not (isinstance(old, dict) and isinstance(new, dict)):
        return f"{_fmt(old)} → {_fmt(new)}"
    om, nm = old.get("members", {}), new.get("members", {})
    added = sorted(set(nm) - set(om))
    removed = sorted(set(om) - set(nm))
    changed = sorted(k for k in set(om) & set(nm) if om[k] != nm[k])
    parts = []
    if old.get("kind") != new.get("kind") or old.get("name") != new.get("name"):
        parts.append(f"{_fmt(old)} → {_fmt(new)}")
    if added:
        parts.append("added " + ", ".join(f"`{k}`" for k in added))
    if removed:
        parts.append("removed " + ", ".join(f"`{k}`" for k in removed))
    if changed:
        parts.append("renumbered " + ", ".join(f"`{k}`" for k in changed))
    return "; ".join(parts) or "unchanged"


def register_section(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    o, n = old["registers"], new["registers"]
    added = sorted(set(n) - set(o), key=lambda k: (n[k]["address"], k))
    removed = sorted(set(o) - set(n), key=lambda k: (o[k]["address"], k))
    lines = [
        f"## huawei-solar register catalog: {old['huawei_solar']} → {new['huawei_solar']}",
        "",
        f"- Registers: {len(o)} → {len(n)} ({len(added)} added, {len(removed)} removed)",
    ]
    changes: list[tuple[str, str, str]] = []
    for key in sorted(set(o) & set(n), key=lambda k: (n[k]["address"], k)):
        for field in REGISTER_FIELDS:
            if o[key].get(field) == n[key].get(field):
                continue
            if field == "unit":
                detail = _members_change(o[key][field], n[key][field])
            else:
                detail = f"{_fmt(o[key].get(field))} → {_fmt(n[key].get(field))}"
            changes.append((key, field, detail))
    lines.append(f"- Changed fields on existing registers: {len(changes)}")
    by_target = Counter(t for k in added for t in n[k]["targets"])
    if by_target:
        lines.append(
            "- Added registers by device: "
            + ", ".join(f"{t} {c}" for t, c in sorted(by_target.items()))
        )
    lines.append("")
    if added:
        lines += ["### Added registers", "", "| Register | Address | Type | Unit | Gain | Writeable | Device |", "|---|---|---|---|---|---|---|"]
        for k in added:
            r = n[k]
            lines.append(
                f"| `{k}` | {r['address']} | {r['type']} | {_fmt(r['unit'])} | {_fmt(r['gain'])} "
                f"| {'yes' if r['writeable'] else 'no'} | {_fmt(r['targets'])} |"
            )
        lines.append("")
    if removed:
        lines += ["### Removed registers", "", "| Register | Address | Device |", "|---|---|---|"]
        for k in removed:
            lines.append(f"| `{k}` | {o[k]['address']} | {_fmt(o[k]['targets'])} |")
        lines.append("")
    if changes:
        lines += ["### Changed registers", "", "| Register | Field | Change |", "|---|---|---|"]
        for key, field, detail in changes:
            lines.append(f"| `{key}` | {field} | {detail} |")
        lines.append("")
    return lines


def entity_section() -> list[str]:
    from emma_catalog import (  # standalone import, avoids loading Home Assistant
        BASELINE_ENTITIES,
        build_entity_catalog,
    )

    current = build_entity_catalog()
    base = BASELINE_ENTITIES
    added = sorted(set(current) - set(base))
    removed = sorted(set(base) - set(current))
    lines = [
        "## Home Assistant entity catalog: 0.14.9 → current",
        "",
        f"- Entities: {len(base)} → {len(current)} ({len(added)} added, {len(removed)} no longer created)",
    ]
    platform_changes = []
    enable_changes = []
    for key in sorted(set(base) & set(current)):
        b_platform, b_enabled = base[key][0], base[key][1]
        desc = current[key]
        if b_platform != desc.platform:
            platform_changes.append((key, b_platform, desc.platform))
        if bool(b_enabled) != bool(desc.enabled_default):
            enable_changes.append((key, b_enabled, desc.enabled_default))
    lines += [
        f"- Platform changes (unique ID and object ID kept; the registry migration moves the entity to the sensor domain): {len(platform_changes)}",
        f"- Default-enabled changes (existing user-disabled entities stay disabled): {len(enable_changes)}",
        "",
    ]
    if platform_changes:
        lines += ["### Platform changes", "", "| Register | 0.14.9 | 0.15.0 |", "|---|---|---|"]
        lines += [f"| `{k}` | {a} | {b} |" for k, a, b in platform_changes]
        lines.append("")
    if enable_changes:
        lines += ["### Default enablement changes", "", "| Register | 0.14.9 | 0.15.0 |", "|---|---|---|"]
        lines += [f"| `{k}` | {a} | {b} |" for k, a, b in enable_changes]
        lines.append("")
    if added:
        by_role = Counter(current[k].device_role for k in added)
        lines += [
            "### Added entities",
            "",
            "By device role: " + ", ".join(f"{r} {c}" for r, c in sorted(by_role.items())),
            "",
            "| Register | Platform | Device role | Enabled by default |",
            "|---|---|---|---|",
        ]
        for k in added:
            d = current[k]
            lines.append(f"| `{k}` | {d.platform} | {d.device_role} | {'yes' if d.enabled_default else 'no'} |")
        lines.append("")
    if removed:
        lines += ["### Entities no longer created", "", "| Register | 0.14.9 platform |", "|---|---|"]
        lines += [f"| `{k}` | {base[k][0]} |" for k in removed]
        lines.append("")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    args = parser.parse_args()
    old = json.loads(args.old.read_text())
    new = json.loads(args.new.read_text())
    lines = [
        "# Register catalog migration report",
        "",
        "Generated by `tools/register_catalog_diff.py`. Do not edit by hand.",
        "",
    ]
    lines += register_section(old, new)
    lines += entity_section()
    sys.stdout.write("\n".join(lines).rstrip() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
