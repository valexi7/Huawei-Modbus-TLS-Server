#!/usr/bin/env python3
"""Generate ESPHome firmware metadata from the canonical Python catalog."""

from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.metadata
import json
import re
import sys
from dataclasses import is_dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from emma_catalog import (  # noqa: E402
    CAPABILITY_MASKS,
    DERIVED_ENTITY_DESCRIPTIONS,
    SMARTGUARD_BINARY_DESCRIPTIONS,
    VIRTUAL_ENTITY_DESCRIPTIONS,
    build_entity_catalog,
    catalog_sha256,
)
from connector_contract import (  # noqa: E402
    API_PREFIX,
    CONNECTOR_VERSION,
    CONTRACT_VERSION,
    DEFAULT_API_PORT,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_TLS_PORT,
    MAX_HTTP_REQUEST_BODY,
    MAX_MBAP_LENGTH,
    MAX_READ_REGISTERS,
    MAX_WRITE_REGISTERS,
    POLL_INTERVALS,
    TOU_MAX_PERIODS,
)
from huawei_solar.registers import REGISTERS  # noqa: E402


HUAWEI_SOLAR_VERSION = importlib.metadata.version("huawei-solar")
DEFAULT_HEADER = ROOT / "esphome/components/huawei_emma_reverse/generated_register_catalog.h"
DEFAULT_MANIFEST = ROOT / "esphome/entity-migration-map.json"
DEFAULT_CONTRACT = ROOT / "esphome/connector-contract.json"
DEFAULT_PY_CONTRACT = ROOT / "esphome/components/huawei_emma_reverse/generated_contract.py"


def _esphome_id(register_name: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "_", register_name.lower()).strip("_")
    return f"emma_{value}"


def _entry(description: dict[str, Any], *, virtual: bool) -> dict[str, Any]:
    result = dict(description)
    if isinstance(result.get("options"), tuple):
        result["options"] = list(result["options"])
    register_name = str(result["register_name"])
    result.update(
        {
            "logical_id": register_name,
            "esphome_id": _esphome_id(register_name),
            "hacs_unique_id_template": "{emma_serial}_" + register_name,
            "entity_owner": "huawei_emma_management",
            "firmware_supported": not virtual,
            "virtual": virtual,
        }
    )
    return result


def connector_contract() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "api_prefix": API_PREFIX,
        "defaults": {
            "tls_port": DEFAULT_TLS_PORT,
            "api_port": DEFAULT_API_PORT,
            "request_timeout_seconds": DEFAULT_REQUEST_TIMEOUT_SECONDS,
        },
        "limits": {
            "http_request_body": MAX_HTTP_REQUEST_BODY,
            "mbap_length": MAX_MBAP_LENGTH,
            "read_registers": MAX_READ_REGISTERS,
            "write_registers": MAX_WRITE_REGISTERS,
            "tou_periods": TOU_MAX_PERIODS,
        },
        "poll_intervals_seconds": POLL_INTERVALS,
    }


def build_manifest() -> dict[str, Any]:
    catalog = build_entity_catalog()
    physical = [
        _entry(description.to_dict(), virtual=False)
        for description in sorted(catalog.values(), key=lambda x: (x.address, x.register_name))
    ]
    virtual = [
        _entry(dict(item), virtual=True)
        for item in (
            *VIRTUAL_ENTITY_DESCRIPTIONS.values(),
            *DERIVED_ENTITY_DESCRIPTIONS.values(),
            *SMARTGUARD_BINARY_DESCRIPTIONS.values(),
        )
    ]
    entities = physical + virtual
    canonical = json.dumps(entities, sort_keys=True, separators=(",", ":"))
    return {
        "schema_version": 3,
        "connector_contract_version": CONTRACT_VERSION,
        # Same hash as the Python connector's CATALOG_SHA256, so diagnostics
        # from every runtime can be compared directly.
        "catalog_sha256": catalog_sha256(catalog),
        "manifest_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "source": {
            "python_module": "custom_components.huawei_emma_management.embedded_catalog",
            "contract_module": "custom_components.huawei_emma_management.connector_contract",
            "register_library": f"huawei-solar=={HUAWEI_SOLAR_VERSION}",
            "generator": "tools/generate_esphome_catalog.py",
        },
        "entity_policy": {
            "owner": "huawei_emma_management",
            "logical_id": "register_name",
            "hacs_unique_id": "{emma_serial}_{register_name}",
            "esphome_id": "emma_{register_name}",
            "migration": "Keep the HACS integration as entity owner and change only its connector host.",
        },
        "physical_entity_count": len(physical),
        "virtual_entity_count": len(virtual),
        "firmware_supported_count": len(physical),
        "entities": entities,
    }


def _cpp_string(value: Any) -> str:
    if value is None:
        value = ""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


VALUE_TYPES = {
    "U16Register": "U16",
    "I16Register": "I16",
    "U32Register": "U32",
    "I32Register": "I32",
    "U64Register": "U64",
    "I64Register": "I64",
    "I32AbsoluteValueRegister": "I32_ABSOLUTE",
    "StringRegister": "STRING",
    "TimestampRegister": "TIMESTAMP",
    "IPv4Register": "IPV4",
    "HUAWEI_LUNA2000_TimeOfUseRegisters": "TOU_HUAWEI",
    "LG_RESU_TimeOfUseRegisters": "TOU_LG",
    "ChargeDischargePeriodRegisters": "CHARGE_PERIODS",
    "PeakSettingPeriodRegisters": "PEAK_PERIODS",
}


def _unit_kind(unit: Any) -> str:
    if unit is bool:
        return "BOOL"
    if isinstance(unit, type) and issubclass(unit, IntEnum):
        return "ENUM"
    if isinstance(unit, dict):
        return "MAP"
    if isinstance(unit, functools.partial):
        return "BITFIELD"
    return "PLAIN"


def _mapping_rows(register_name: str, description: Any) -> list[tuple[int, str, str, str]]:
    register = REGISTERS[register_name]
    unit = getattr(register, "unit", None)
    rows: list[tuple[int, str, str, str]] = []
    if isinstance(unit, type) and issubclass(unit, IntEnum):
        by_value = {int(option["value"]): option for option in description.options}
        for member in unit:
            option = by_value[int(member.value)]
            rows.append((int(member.value), str(option["key"]), str(option["label"]), ""))
    elif isinstance(unit, dict):
        rows.extend((int(raw), str(label), str(label), "") for raw, label in unit.items())
    elif isinstance(unit, functools.partial) and unit.args and isinstance(unit.args[0], dict):
        for mask, value in unit.args[0].items():
            if isinstance(value, str):
                rows.append((int(mask), value, value, ""))
            elif hasattr(value, "on_value"):
                rows.append((int(mask), str(value.on_value), str(value.on_value), str(value.off_value)))
            elif is_dataclass(value) or hasattr(value, "name"):
                label = str(getattr(value, "name", value))
                rows.append((int(mask), label, label, ""))
    return rows


def _extra_json(description: Any) -> str:
    """Return 0.15 entity metadata as a JSON object fragment (leading comma).

    The firmware streams this verbatim in /entities so new metadata stays in
    exact parity with the Python connector without hand-written C++.
    """
    extra: dict[str, Any] = {}
    for field in ("value_kind", "allowed_values", "required_capabilities", "depends_on", "flags"):
        value = getattr(description, field)
        if value:
            extra[field] = list(value) if isinstance(value, tuple) else value
    if description.three_phase_only:
        extra["three_phase_only"] = True
    if not extra:
        return ""
    return "," + json.dumps(extra, separators=(",", ":"), ensure_ascii=False)[1:-1]


def build_header(manifest: dict[str, Any]) -> str:
    descriptions = build_entity_catalog()
    names = sorted(descriptions, key=lambda name: (descriptions[name].address, name))
    index_of = {name: index for index, name in enumerate(names)}
    capability_names = list(CAPABILITY_MASKS)
    allowed: list[tuple[int, int]] = []
    allowed_ranges: dict[str, tuple[int, int]] = {}
    dependencies: list[tuple[int, int]] = []
    dependency_ranges: dict[str, tuple[int, int]] = {}
    for name in names:
        values = descriptions[name].allowed_values
        allowed_ranges[name] = (len(allowed), len(values))
        allowed.extend((index_of[name], int(value)) for value in values)
        depends = [dep for dep in descriptions[name].depends_on if dep in index_of]
        dependency_ranges[name] = (len(dependencies), len(depends))
        dependencies.extend((index_of[name], index_of[dep]) for dep in depends)
    mappings: list[tuple[int, int, str, str, str]] = []
    mapping_ranges: dict[str, tuple[int, int]] = {}
    for entity_index, name in enumerate(names):
        start = len(mappings)
        mappings.extend((entity_index, *row) for row in _mapping_rows(name, descriptions[name]))
        mapping_ranges[name] = (start, len(mappings) - start)

    contract = connector_contract()
    lines = [
        "// Generated by tools/generate_esphome_catalog.py; do not edit.",
        "#pragma once",
        "",
        "#include <array>",
        "#include <cstddef>",
        "#include <cstdint>",
        "",
        "namespace esphome::huawei_emma_reverse {",
        "",
        "enum class GeneratedValueType : uint8_t { U16, I16, U32, I32, U64, I64, I32_ABSOLUTE, STRING, TIMESTAMP, IPV4, TOU_HUAWEI, TOU_LG, CHARGE_PERIODS, PEAK_PERIODS };",
        "enum class GeneratedUnitKind : uint8_t { PLAIN, BOOL, ENUM, MAP, BITFIELD };",
        "",
        "struct GeneratedValueMapping {",
        "  uint16_t entity_index; int64_t raw; const char *key; const char *label; const char *off_label;",
        "};",
        "",
        "struct GeneratedEntityMetadata {",
        "  const char *register_name; const char *esphome_id; const char *name;",
        "  uint16_t address; uint16_t length;",
        "  const char *platform; const char *poll_group; const char *device_role; const char *client_role;",
        "  const char *unit; const char *device_class; const char *state_class; const char *entity_category; const char *icon;",
        "  bool enabled_default; bool writeable; const char *format;",
        "  GeneratedValueType value_type; GeneratedUnitKind unit_kind; double gain;",
        "  uint64_t invalid_raw; bool has_invalid;",
        "  double minimum; double maximum; double step; bool has_range;",
        "  uint16_t mapping_start; uint16_t mapping_count;",
        "  uint16_t allowed_start; uint8_t allowed_count;",
        "  int16_t required_capability; bool three_phase_only; bool capability_mask;",
        "  uint16_t dependency_start; uint8_t dependency_count;",
        "  const char *extra_json;",
        "};",
        "",
        "struct GeneratedCapability {",
        "  const char *name; uint16_t mask_entity_index; uint8_t bit; bool inverted;",
        "};",
        "",
        "struct GeneratedIndexPair {",
        "  uint16_t entity_index; int64_t value;",
        "};",
        "",
        f"inline constexpr uint16_t GENERATED_CONTRACT_VERSION = {CONTRACT_VERSION};",
        f'inline constexpr char GENERATED_API_PREFIX[] = "{API_PREFIX}";',
        f'inline constexpr char GENERATED_PATH_HEALTH[] = "{API_PREFIX}/health";',
        f'inline constexpr char GENERATED_PATH_DEVICE[] = "{API_PREFIX}/device";',
        f'inline constexpr char GENERATED_PATH_ENTITIES[] = "{API_PREFIX}/entities";',
        f'inline constexpr char GENERATED_PATH_STATES[] = "{API_PREFIX}/states";',
        f'inline constexpr char GENERATED_PATH_SUBSCRIPTIONS[] = "{API_PREFIX}/subscriptions";',
        f'inline constexpr char GENERATED_PATH_TOU[] = "{API_PREFIX}/tou-periods";',
        f'inline constexpr char GENERATED_PATH_ENTITY_WILDCARD[] = "{API_PREFIX}/entities/*";',
        f'inline constexpr char GENERATED_CATALOG_SHA256[] = "{manifest["catalog_sha256"]}";',
        f"inline constexpr uint16_t GENERATED_DEFAULT_TLS_PORT = {DEFAULT_TLS_PORT};",
        f"inline constexpr uint16_t GENERATED_DEFAULT_API_PORT = {DEFAULT_API_PORT};",
        f"inline constexpr uint32_t GENERATED_FAST_POLL_MS = {POLL_INTERVALS['fast'] * 1000}UL;",
        f"inline constexpr uint32_t GENERATED_MEDIUM_POLL_MS = {POLL_INTERVALS['medium'] * 1000}UL;",
        f"inline constexpr uint32_t GENERATED_SLOW_POLL_MS = {POLL_INTERVALS['slow'] * 1000}UL;",
        f"inline constexpr size_t GENERATED_MAX_HTTP_BODY = {MAX_HTTP_REQUEST_BODY};",
        f"inline constexpr size_t GENERATED_MAX_MBAP_LENGTH = {MAX_MBAP_LENGTH};",
        f"inline constexpr uint16_t GENERATED_MAX_READ_REGISTERS = {MAX_READ_REGISTERS};",
        f"inline constexpr uint16_t GENERATED_MAX_WRITE_REGISTERS = {MAX_WRITE_REGISTERS};",
        f"inline constexpr uint8_t GENERATED_TOU_MAX_PERIODS = {TOU_MAX_PERIODS};",
        f'inline constexpr char GENERATED_PATH_DIAGNOSTICS[] = "{API_PREFIX}/diagnostics";',
        f'inline constexpr char GENERATED_HUAWEI_SOLAR_VERSION[] = "{HUAWEI_SOLAR_VERSION}";',
        f'inline constexpr char GENERATED_CONNECTOR_VERSION[] = "{CONNECTOR_VERSION}";',
        f"inline constexpr size_t GENERATED_ENTITY_COUNT = {len(names)};",
        f"inline constexpr size_t GENERATED_MAPPING_COUNT = {len(mappings)};",
        f"inline constexpr size_t GENERATED_CAPABILITY_COUNT = {len(capability_names)};",
        f"inline constexpr size_t GENERATED_ALLOWED_COUNT = {max(len(allowed), 1)};",
        f"inline constexpr size_t GENERATED_DEPENDENCY_COUNT = {max(len(dependencies), 1)};",
        "",
        "inline constexpr std::array<GeneratedEntityMetadata, GENERATED_ENTITY_COUNT> GENERATED_ENTITIES{{",
    ]
    mask_registers = {mask for mask, _bit, _inverted in CAPABILITY_MASKS.values()}
    for name in names:
        description = descriptions[name]
        register = REGISTERS[name]
        class_name = type(register).__name__
        if class_name not in VALUE_TYPES:
            raise ValueError(f"Unsupported firmware register type {class_name}: {name}")
        start, count = mapping_ranges[name]
        invalid = getattr(register, "invalid_value", None)
        minimum = 0 if description.minimum is None else description.minimum
        maximum = 0 if description.maximum is None else description.maximum
        step = 0 if description.step is None else description.step
        fields = (
            _cpp_string(name), _cpp_string(_esphome_id(name)), _cpp_string(description.name),
            str(description.address), str(description.length), _cpp_string(description.platform),
            _cpp_string(description.poll_group), _cpp_string(description.device_role),
            _cpp_string(description.client_role), _cpp_string(description.unit),
            _cpp_string(description.device_class), _cpp_string(description.state_class),
            _cpp_string(description.entity_category), _cpp_string(description.icon),
            "true" if description.enabled_default else "false",
            "true" if description.writeable else "false", _cpp_string(description.format),
            f"GeneratedValueType::{VALUE_TYPES[class_name]}",
            f"GeneratedUnitKind::{_unit_kind(getattr(register, 'unit', None))}",
            f"{float(getattr(register, 'gain', 1)):.12g}",
            f"{(0 if invalid is None else int(invalid) & ((1 << 64) - 1))}ULL",
            "false" if invalid is None else "true", f"{minimum:.12g}", f"{maximum:.12g}",
            f"{step:.12g}", "true" if description.minimum is not None else "false", str(start), str(count),
            str(allowed_ranges[name][0]), str(allowed_ranges[name][1]),
            str(_capability_index(description, capability_names)),
            "true" if description.three_phase_only else "false",
            "true" if name in mask_registers else "false",
            str(dependency_ranges[name][0]), str(dependency_ranges[name][1]),
            _cpp_string(_extra_json(description)),
        )
        lines.append("    {" + ", ".join(fields) + "},")
    lines.extend(["}};", "", "inline constexpr std::array<GeneratedValueMapping, GENERATED_MAPPING_COUNT> GENERATED_MAPPINGS{{"])
    for entity_index, raw, key, label, off_label in mappings:
        lines.append(f"    {{{entity_index}, {raw}, {_cpp_string(key)}, {_cpp_string(label)}, {_cpp_string(off_label)}}},")
    lines.extend(["}};", "", "inline constexpr std::array<GeneratedCapability, GENERATED_CAPABILITY_COUNT> GENERATED_CAPABILITIES{{"])
    for capability in capability_names:
        mask, bit, inverted = CAPABILITY_MASKS[capability]
        lines.append(
            f"    {{{_cpp_string(capability)}, {index_of[mask]}, {bit}, {'true' if inverted else 'false'}}},"
        )
    lines.extend(["}};", "", "inline constexpr std::array<GeneratedIndexPair, GENERATED_ALLOWED_COUNT> GENERATED_ALLOWED_VALUES{{"])
    for entity_index, value in allowed or [(0, 0)]:
        lines.append(f"    {{{entity_index}, {value}}},")
    lines.extend(["}};", "", "inline constexpr std::array<GeneratedIndexPair, GENERATED_DEPENDENCY_COUNT> GENERATED_DEPENDENCIES{{"])
    for entity_index, dependency in dependencies or [(0, 0)]:
        lines.append(f"    {{{entity_index}, {dependency}}},")
    lines.extend(["}};", "", "}  // namespace esphome::huawei_emma_reverse", ""])
    return "\n".join(lines)


def _capability_index(description: Any, capability_names: list[str]) -> int:
    capabilities = description.required_capabilities
    if len(capabilities) > 1:
        raise ValueError(
            f"Firmware supports one required capability per register: {description.register_name}"
        )
    return capability_names.index(capabilities[0]) if capabilities else -1


def generated_outputs() -> dict[Path, str]:
    manifest = build_manifest()
    python_contract = (
        "# Generated by tools/generate_esphome_catalog.py; do not edit.\n"
        f"CONTRACT_VERSION = {CONTRACT_VERSION}\n"
        f"DEFAULT_TLS_PORT = {DEFAULT_TLS_PORT}\n"
        f"DEFAULT_API_PORT = {DEFAULT_API_PORT}\n"
    )
    return {
        DEFAULT_MANIFEST: json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        DEFAULT_CONTRACT: json.dumps(connector_contract(), indent=2) + "\n",
        DEFAULT_PY_CONTRACT: python_contract,
        DEFAULT_HEADER: build_header(manifest),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale: list[Path] = []
    for path, content in generated_outputs().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(path.relative_to(ROOT))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"Generated {path.relative_to(ROOT)}")
    if stale:
        raise SystemExit("ESPHome artifacts are stale: " + ", ".join(map(str, stale)))


if __name__ == "__main__":
    main()
