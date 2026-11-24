"""Huawei EMMA register metadata shared by embedded and external connectors."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
import hashlib
from enum import IntEnum, IntFlag
from ipaddress import IPv4Address
import json
from pathlib import Path
import re
from typing import Any

from huawei_solar import register_names as rn
from huawei_solar.register_definitions import TargetDevice
from huawei_solar.registers import REGISTERS

try:
    from .connector_contract import POLL_INTERVALS
except ImportError:  # Standalone generator compatibility.
    from connector_contract import POLL_INTERVALS


SUN2000_EXPOSED_REGISTERS = {
    str(rn.STORAGE_MAXIMUM_CHARGING_POWER),
    str(rn.STORAGE_MAXIMUM_DISCHARGING_POWER),
    str(rn.STORAGE_CHARGING_CUTOFF_CAPACITY),
    str(rn.STORAGE_DISCHARGING_CUTOFF_CAPACITY),
    str(rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD),
    str(rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_POWER),
    str(rn.STORAGE_WORKING_MODE_SETTINGS),
    str(rn.STORAGE_CHARGE_FROM_GRID_FUNCTION),
    str(rn.STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE),
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE),
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SOC),
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE),
    str(rn.STORAGE_FORCIBLE_CHARGE_POWER),
    str(rn.STORAGE_FORCIBLE_DISCHARGE_POWER),
    str(rn.STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU),
}

FRIENDLY_NAME_OVERRIDES = {
    str(rn.EMMA_TOU_PREFERRED_USE_OF_SURPLUS_PV_POWER): "PV Power Priority",
    str(rn.STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU): "PV Power Priority",
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE): "Forced Charge/Discharge",
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE): "Forced Charge/Discharge Target Mode",
}

UNIT_OVERRIDES = {
    # huawei-solar currently leaves these two U32 definitions unitless, but
    # Huawei specifies and displays both values as watts.
    str(rn.STORAGE_FORCIBLE_CHARGE_POWER): "W",
    str(rn.STORAGE_FORCIBLE_DISCHARGE_POWER): "W",
}

UNIT_NORMALIZATION = {
    "kW h": "kWh",
    "kVa r": "kVar",
    "kvar h": "kvarh",
    "kg/k Wh": "kg/kWh",
    "mins": "min",
    "minutes": "min",
    "seconds": "s",
    "hour": "h",
    "MOhm": "MΩ",
}


@dataclass(frozen=True, slots=True)
class EntityDescription:
    register_name: str
    name: str
    address: int
    length: int
    platform: str
    poll_group: str
    device_role: str = "emma"
    client_role: str = "emma"
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    entity_category: str | None = None
    icon: str | None = None
    enabled_default: bool = True
    writeable: bool = False
    options: tuple[dict[str, Any], ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    format: str | None = None
    value_kind: str | None = None
    allowed_values: tuple[int, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    three_phase_only: bool = False
    flags: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Virtual Home Assistant entities derived from the EMMA TOU register. Keeping
# their metadata beside the Modbus catalog gives the ESPHome generator one
# dependency-free source for every entity name and logical ID.
VIRTUAL_ENTITY_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    "emma_tou_schedule_text": {
        "register_name": "emma_tou_schedule_text",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 1. Active Schedule",
        "platform": "sensor",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "diagnostic",
        "icon": "mdi:calendar-clock",
        "enabled_default": True,
        "address": 40004,
        "format": "tou_active_schedule_text",
    },
    "emma_tou_planned_schedule_text": {
        "register_name": "emma_tou_planned_schedule_text",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 2. Planned Schedule",
        "platform": "sensor",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "diagnostic",
        "icon": "mdi:calendar-edit",
        "enabled_default": True,
        "address": 40004,
        "format": "tou_planned_schedule_text",
    },
    "tou_selected_period": {
        "register_name": "tou_selected_period",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 3. Select Period",
        "platform": "select",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "config",
        "icon": "mdi:format-list-numbered",
        "enabled_default": True,
        "address": 40004,
    },
    "tou_start_time": {
        "register_name": "tou_start_time",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 4. Period Start Time",
        "platform": "time",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "config",
        "icon": "mdi:clock-outline",
        "enabled_default": True,
        "address": 40004,
    },
    "tou_end_time": {
        "register_name": "tou_end_time",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 5. Period End Time",
        "platform": "time",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "config",
        "icon": "mdi:clock-outline",
        "enabled_default": True,
        "address": 40004,
    },
    "tou_mode": {
        "register_name": "tou_mode",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 6. Period Mode",
        "platform": "select",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "config",
        "icon": "mdi:battery-sync",
        "enabled_default": True,
        "address": 40004,
    },
    "tou_enabled": {
        "register_name": "tou_enabled",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 7. Enabled",
        "platform": "switch",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "config",
        "icon": "mdi:calendar-check",
        "enabled_default": True,
        "address": 40004,
    },
    "tou_clear_period": {
        "register_name": "tou_clear_period",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 8. Clear Period",
        "platform": "button",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "config",
        "icon": "mdi:calendar-remove",
        "enabled_default": True,
        "address": 40004,
    },
    "tou_apply_schedule": {
        "register_name": "tou_apply_schedule",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 9. Apply Schedule",
        "platform": "button",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "config",
        "icon": "mdi:content-save-check",
        "enabled_default": True,
        "address": 40004,
    },
    "tou_plan_json": {
        "register_name": "tou_plan_json",
        "source_register_name": str(rn.EMMA_TOU_PERIODS),
        "name": "TOU 10. Plan JSON",
        "platform": "text",
        "poll_group": "medium",
        "device_role": "emma",
        "client_role": "emma",
        "entity_category": "diagnostic",
        "icon": "mdi:code-json",
        "enabled_default": True,
        "address": 40004,
    },
}


# Sign conventions of EMMA aggregate power registers (connector API values are
# never inverted). feed_in_power is import-positive as reported in BESS Manager
# issue #818; battery_charge_discharge_power is charge-positive like SUN2000
# register 37765/47321. Both require confirmation on real hardware.
EMMA_GRID_POWER_REGISTER = str(rn.FEED_IN_POWER)
EMMA_GRID_POWER_POLARITY = "import_positive"
EMMA_BATTERY_POWER_REGISTER = str(rn.BATTERY_CHARGE_DISCHARGE_POWER)
EMMA_BATTERY_POWER_POLARITY = "charge_positive"


def _derived(key: str, name: str, source: str, polarity: str, positive: bool, icon: str) -> dict[str, Any]:
    return {
        "register_name": key,
        "source_register_name": source,
        "source_polarity": polarity,
        "take_positive": positive,
        "name": name,
        "platform": "sensor",
        "poll_group": "fast",
        "device_role": "emma",
        "client_role": "emma",
        "unit": "W",
        "device_class": "power",
        "state_class": "measurement",
        "icon": icon,
        # Optional normalized views: disabled by default like other new entities.
        "enabled_default": False,
        "address": REGISTERS[source].register,
        "format": "derived_power",
    }


# Directional power entities derived from existing polled values. They never
# generate Modbus requests and keep the raw signed registers untouched.
DERIVED_ENTITY_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    item["register_name"]: item
    for item in (
        _derived(
            "derived_grid_import_power",
            "Grid Import Power",
            EMMA_GRID_POWER_REGISTER,
            EMMA_GRID_POWER_POLARITY,
            True,
            "mdi:transmission-tower-import",
        ),
        _derived(
            "derived_grid_export_power",
            "Grid Export Power",
            EMMA_GRID_POWER_REGISTER,
            EMMA_GRID_POWER_POLARITY,
            False,
            "mdi:transmission-tower-export",
        ),
        _derived(
            "derived_battery_charging_power",
            "Battery Charging Power",
            EMMA_BATTERY_POWER_REGISTER,
            EMMA_BATTERY_POWER_POLARITY,
            True,
            "mdi:battery-arrow-up",
        ),
        _derived(
            "derived_battery_discharging_power",
            "Battery Discharging Power",
            EMMA_BATTERY_POWER_REGISTER,
            EMMA_BATTERY_POWER_POLARITY,
            False,
            "mdi:battery-arrow-down",
        ),
    )
}


def derived_power_value(description: dict[str, Any], value: Any) -> float | None:
    """Return the non-negative directional part of a signed power value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    signed = float(value)
    return max(signed, 0.0) if description["take_positive"] else max(-signed, 0.0)


# SmartGuard operating status interpretation (upstream SmartGuardStatus,
# register 30509). Each derived binary sensor is True/False only for statuses
# whose meaning is unambiguous and None (unknown) otherwise. An unavailable or
# stale SmartGuard is never interpreted as an outage or as normal operation.
SMARTGUARD_OFF_GRID_STATUSES = frozenset(
    {"off_grid_generator", "off_grid_inverter", "off_grid_generator_and_inverter"}
)
SMARTGUARD_STATUS_CATEGORIES: dict[str, str] = {
    "standby": "transitional",
    "on_grid": "grid",
    "off_grid_generator": "backup_generator",
    "off_grid_inverter": "backup_inverter",
    "off_grid_generator_and_inverter": "backup_generator_and_inverter",
    "bypassed": "bypassed",
    "unexpected_shutdown": "fault",
    "unexpected_shutdown_grid_supply": "fault",
}


def _smartguard_binary(key: str, name: str, device_class: str | None, icon: str) -> dict[str, Any]:
    return {
        "register_name": key,
        "source_register_name": str(rn.SMARTGUARD_STATUS),
        "name": name,
        "platform": "binary_sensor",
        "poll_group": "fast",
        "device_role": "smartguard",
        "client_role": "smartguard",
        "device_class": device_class,
        "icon": icon,
        "enabled_default": True,
        "address": REGISTERS[rn.SMARTGUARD_STATUS].register,
        "format": "smartguard_status_flag",
    }


SMARTGUARD_BINARY_DESCRIPTIONS: dict[str, dict[str, Any]] = {
    item["register_name"]: item
    for item in (
        _smartguard_binary(
            "smartguard_grid_connected", "Grid Connected", "power", "mdi:transmission-tower"
        ),
        _smartguard_binary(
            "smartguard_off_grid_operation", "Off-Grid Operation", None, "mdi:home-battery"
        ),
        _smartguard_binary(
            "smartguard_inverter_backup_supply",
            "Inverter Backup Supply",
            None,
            "mdi:battery-charging-wireless",
        ),
        _smartguard_binary(
            "smartguard_generator_supply", "Generator Supply", None, "mdi:engine"
        ),
    )
}


def smartguard_status_flag(key: str, status: Any) -> bool | None:
    """Interpret a SmartGuard status for one derived binary sensor."""
    if not isinstance(status, str) or status not in SMARTGUARD_STATUS_CATEGORIES:
        return None
    if key == "smartguard_grid_connected":
        if status == "on_grid":
            return True
        return False if status in SMARTGUARD_OFF_GRID_STATUSES else None
    if key == "smartguard_off_grid_operation":
        if status in SMARTGUARD_OFF_GRID_STATUSES:
            return True
        return False if status == "on_grid" else None
    if key == "smartguard_inverter_backup_supply":
        if status in ("off_grid_inverter", "off_grid_generator_and_inverter"):
            return True
        return False if status in ("on_grid", "off_grid_generator") else None
    if key == "smartguard_generator_supply":
        if status in ("off_grid_generator", "off_grid_generator_and_inverter"):
            return True
        return False if status in ("on_grid", "off_grid_inverter") else None
    raise KeyError(key)


SAFE_NUMBER_RANGES: dict[str, tuple[float, float, float]] = {
    str(rn.EMMA_TOU_MAXIMUM_POWER_FOR_CHARGING_BATTERIES_FROM_GRID): (0, 50_000, 100),
    str(rn.EMMA_MAXIMUM_FEED_GRID_POWER_WATT): (-1, 100_000, 100),
    str(rn.EMMA_MAXIMUM_FEED_GRID_POWER_PERCENT): (0, 100, 0.1),
    str(rn.EMMA_SYSTEM_TIME): (0, 4_294_967_295, 1),
    str(rn.LOCAL_TIME_YEAR): (2000, 2099, 1),
    str(rn.STORAGE_MAXIMUM_CHARGING_POWER): (200, 100_000, 1),
    str(rn.STORAGE_MAXIMUM_DISCHARGING_POWER): (200, 100_000, 1),
    str(rn.STORAGE_CHARGING_CUTOFF_CAPACITY): (90, 100, 1),
    str(rn.STORAGE_DISCHARGING_CUTOFF_CAPACITY): (0, 20, 1),
    str(rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD): (0, 1440, 1),
    str(rn.STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE): (20, 100, 1),
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SOC): (0, 100, 1),
    str(rn.STORAGE_FORCIBLE_CHARGE_POWER): (0, 100_000, 1),
    str(rn.STORAGE_FORCIBLE_DISCHARGE_POWER): (0, 100_000, 1),
}


# ---------------------------------------------------------------------------
# Hardware capabilities derived from upstream feature masks.
#
# Each capability is (mask register, bit, inverted). ``inverted`` marks EMMA
# register 30164, a hide/disable mask where a set bit *removes* the feature.
# An unreadable or unsupported mask leaves the capability unknown; unknown is
# treated as "allowed" for controls that existed before 0.15.0, matching the
# upstream huawei-solar EMMADevice fallback for firmware without 30164.
# ---------------------------------------------------------------------------
CAPABILITY_MASKS: dict[str, tuple[str, int, bool]] = {
    "emma_power_limited_grid_connection": (str(rn.EMMA_FEATURE_MASK_30164), 3, True),
    "emma_di_scheduling": (str(rn.EMMA_FEATURE_MASK_30164), 4, True),
    "emma_capacity_control": (str(rn.EMMA_FEATURE_MASK_30164), 5, True),
    "emma_three_phase_unbalance_control": (str(rn.EMMA_FEATURE_MASK_30122), 20, False),
    "emma_backup_seamless_switch": (str(rn.EMMA_FEATURE_MASK_30122), 6, False),
    "emma_generator_and_mains_anomaly_detection": (
        str(rn.EMMA_FEATURE_MASK_30122),
        12,
        False,
    ),
    "emma_manual_off_grid_switchover": (str(rn.EMMA_FEATURE_MASK_30124), 9, False),
    "emma_charge_from_ac": (str(rn.EMMA_FEATURE_MASK_30124), 15, False),
    "emma_off_grid_backup_power_soc": (str(rn.EMMA_FEATURE_MASK_30172), 4, False),
    "emma_battery_eod_eoc_soc": (str(rn.EMMA_FEATURE_MASK_30172), 7, False),
    "emma_off_grid_mode": (str(rn.EMMA_FEATURE_MASK_30172), 11, False),
    "smartguard_grid_switchover": (str(rn.SMARTGUARD_FEATURE_MASK), 0, False),
    "sun2000_ess_low_power": (str(rn.FEATURE_MASK_30342), 4, False),
    "sun2000_third_party_control": (str(rn.FEATURE_MASK_30283), 8, False),
    "sun2000_battery_recharge": (str(rn.FEATURE_MASK_30306), 9, False),
    "sun2000_discharge_power_adjustment": (str(rn.FEATURE_MASK_30306), 15, False),
    "sun2000_capacity_control": (str(rn.FEATURE_MASK_3), 30, False),
}

# Feature-mask registers polled internally (slow group) whenever their device
# is present, independently of the visibility of their diagnostic entities.
CAPABILITY_MASK_REGISTERS = tuple(sorted({mask for mask, _, _ in CAPABILITY_MASKS.values()}))


def capability_state(capability: str, values: dict[str, Any]) -> bool | None:
    """Return True/False when a feature mask is known, otherwise None."""
    mask_register, bit, inverted = CAPABILITY_MASKS[capability]
    raw = values.get(mask_register)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    present = bool(raw & (1 << bit))
    return not present if inverted else present


def capability_states(values: dict[str, Any]) -> dict[str, bool | None]:
    """Return every known capability state for diagnostics."""
    return {name: capability_state(name, values) for name in CAPABILITY_MASKS}


# ---------------------------------------------------------------------------
# Reviewed write allowlist.
#
# Readable by default; writable only after explicit review. Upstream marks
# many registers writeable (network settings, factory resets, protection
# thresholds, alarm clearing, grid switching). None of those become controls
# unless they are listed here with their allowed values, limits, routing and
# required capabilities. Every write is followed by a readback comparison in
# the connector before success is reported.
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class WritePolicy:
    platform: str
    allowed_values: tuple[int, ...] = ()
    required_capabilities: tuple[str, ...] = ()


WRITE_ALLOWLIST: dict[str, WritePolicy] = {
    # EMMA energy management controls (0.14.x supported controls).
    str(rn.EMMA_ESS_CONTROL_MODE): WritePolicy("select", (2, 4, 5, 6)),
    str(rn.EMMA_TOU_PREFERRED_USE_OF_SURPLUS_PV_POWER): WritePolicy("select", (0, 1)),
    str(rn.EMMA_TOU_MAXIMUM_POWER_FOR_CHARGING_BATTERIES_FROM_GRID): WritePolicy("number"),
    str(rn.EMMA_TOU_PERIODS): WritePolicy("sensor"),
    str(rn.EMMA_POWER_CONTROL_MODE_AT_GRID_CONNECTION_POINT): WritePolicy(
        "select", (0, 5, 6, 7), ("emma_power_limited_grid_connection",)
    ),
    str(rn.EMMA_LIMITATION_MODE): WritePolicy(
        "select", (0, 1), ("emma_power_limited_grid_connection",)
    ),
    str(rn.EMMA_MAXIMUM_FEED_GRID_POWER_WATT): WritePolicy(
        "number", required_capabilities=("emma_power_limited_grid_connection",)
    ),
    str(rn.EMMA_MAXIMUM_FEED_GRID_POWER_PERCENT): WritePolicy(
        "number", required_capabilities=("emma_power_limited_grid_connection",)
    ),
    str(rn.EMMA_3PHASE_IMBALANCE_CONTROL): WritePolicy(
        "switch", required_capabilities=("emma_three_phase_unbalance_control",)
    ),
    str(rn.EMMA_SYSTEM_TIME): WritePolicy("datetime"),
    str(rn.LOCAL_TIME_YEAR): WritePolicy("number"),
    str(rn.EMMA_POWER_SUPPLY_CONFIGURATION): WritePolicy("select", (0, 1, 2, 3)),
    str(rn.EMMA_CONSIDER_MAINS_FAULTY_IF): WritePolicy("select", (0, 1)),
    # Native SUN2000/LUNA2000 battery controls routed to the inverter unit ID.
    str(rn.STORAGE_MAXIMUM_CHARGING_POWER): WritePolicy("number"),
    str(rn.STORAGE_MAXIMUM_DISCHARGING_POWER): WritePolicy("number"),
    str(rn.STORAGE_CHARGING_CUTOFF_CAPACITY): WritePolicy("number"),
    str(rn.STORAGE_DISCHARGING_CUTOFF_CAPACITY): WritePolicy("number"),
    str(rn.STORAGE_FORCED_CHARGING_AND_DISCHARGING_PERIOD): WritePolicy("number"),
    # THIRD_PARTY_CONTROL (6) is intentionally not writable: it hands battery
    # dispatch to an external controller and is gated by FeatureMask30283.
    str(rn.STORAGE_WORKING_MODE_SETTINGS): WritePolicy("select", (0, 1, 2, 3, 4, 5)),
    str(rn.STORAGE_CHARGE_FROM_GRID_FUNCTION): WritePolicy("switch"),
    str(rn.STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE): WritePolicy("number"),
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_WRITE): WritePolicy("select", (0, 1, 2)),
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SOC): WritePolicy("number"),
    str(rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SETTING_MODE): WritePolicy("select", (0, 1)),
    str(rn.STORAGE_FORCIBLE_CHARGE_POWER): WritePolicy("number"),
    str(rn.STORAGE_FORCIBLE_DISCHARGE_POWER): WritePolicy("number"),
    str(rn.STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU): WritePolicy("select", (0, 1)),
}

# Controls that 0.14.x exposed automatically only because upstream marked them
# writeable. They are network, protection or maintenance settings without a
# reviewed safe schema and are now read-only diagnostic sensors. The
# integration migrates their registry entries to the sensor platform while
# keeping each user's enabled/disabled choice.
DEMOTED_LEGACY_CONTROLS: dict[str, str] = {
    "mppt_multimodal_scanning": "switch",
    "daylight_saving_time": "switch",
    "sdongle_ssl_encryption": "switch",
    "sdongle_port_mode": "select",
    "sdongle_registration_status": "switch",
    "sdongle_reported_data_record_period": "switch",
    "sdongle_network_mode_4g": "select",
    "wlan_wakeup": "select",
    "active_power_control_mode": "select",
    "remote_charge_discharge_control_mode": "select",
    "backup_voltage_independent_operation": "select",
    "storage_capacity_control_mode": "select",
    "emma": "switch",
}


# ---------------------------------------------------------------------------
# Device routing.
# ---------------------------------------------------------------------------
DEVICE_ROLES = (
    "emma",
    "inverter",
    "smartguard",
    "charger",
    "smart_ac_gun",
    "sdongle",
    "smartlogger",
    "external_meter",
)

_TARGET_ROLES: tuple[tuple[TargetDevice, str], ...] = (
    (TargetDevice.EMMA, "emma"),
    (TargetDevice.SUN2000, "inverter"),
    (TargetDevice.SMARTGUARD, "smartguard"),
    (TargetDevice.SCHARGER, "charger"),
    (TargetDevice.SMART_AC_GUN, "smart_ac_gun"),
    (TargetDevice.SDONGLE, "sdongle"),
    (TargetDevice.SMARTLOGGER, "smartlogger"),
)

# SmartGuard phase registers that only exist on three-phase hardware.
SMARTGUARD_THREE_PHASE_REGISTERS = frozenset(
    str(name)
    for name in (
        rn.SMARTGUARD_GRID_PHASE_A_VOLTAGE,
        rn.SMARTGUARD_GRID_PHASE_B_VOLTAGE,
        rn.SMARTGUARD_GRID_PHASE_C_VOLTAGE,
        rn.SMARTGUARD_GENERATOR_PHASE_A_VOLTAGE,
        rn.SMARTGUARD_GENERATOR_PHASE_B_VOLTAGE,
        rn.SMARTGUARD_GENERATOR_PHASE_C_VOLTAGE,
        rn.SMARTGUARD_BACKUP_LOAD_PHASE_A_VOLTAGE,
        rn.SMARTGUARD_BACKUP_LOAD_PHASE_B_VOLTAGE,
        rn.SMARTGUARD_BACKUP_LOAD_PHASE_C_VOLTAGE,
    )
)

# SmartGuard polling policy (fast = status and voltages, medium = temperature,
# slow = static identity). Applied by name so upstream additions stay safe.
SMARTGUARD_POLL_GROUPS: dict[str, str] = {
    str(rn.SMARTGUARD_STATUS): "fast",
    str(rn.SMARTGUARD_INTERNAL_TEMPERATURE): "medium",
    str(rn.SMARTGUARD_SOFTWARE_VERSION): "slow",
    str(rn.SMARTGUARD_PHASE_TYPE): "slow",
    str(rn.SMARTGUARD_FEATURE_MASK): "slow",
}

# Local wall-clock timestamp registers and the UTC register of the same device
# used to derive the device's UTC offset. Other SUN2000/charger timestamps are
# local wall-clock values on the same site and use the EMMA offset.
UTC_TIMESTAMP_REGISTERS = frozenset(
    {str(rn.EMMA_SYSTEM_TIME), str(rn.SMARTLOGGER_DATE_TIME)}
)
LOCAL_TIMESTAMP_REFERENCES: dict[str, tuple[str, str]] = {
    str(rn.EMMA_LOCAL_TIME): (str(rn.EMMA_LOCAL_TIME), str(rn.EMMA_SYSTEM_TIME)),
    str(rn.SMARTLOGGER_LOCAL_TIME): (
        str(rn.SMARTLOGGER_LOCAL_TIME),
        str(rn.SMARTLOGGER_DATE_TIME),
    ),
}
_SITE_OFFSET_REFERENCE = (str(rn.EMMA_LOCAL_TIME), str(rn.EMMA_SYSTEM_TIME))


def _baseline() -> dict[str, list[Any]]:
    path = Path(__file__).with_name("catalog_baseline_0_14_9.json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))["entities"]
    except (OSError, ValueError, KeyError):  # pragma: no cover - packaging error
        return {}


BASELINE_ENTITIES = _baseline()


def build_entity_catalog() -> dict[str, EntityDescription]:
    catalog: dict[str, EntityDescription] = {}
    for key, register in REGISTERS.items():
        if not register.readable:
            continue
        register_name = str(key)
        class_name = register.__class__.__name__
        baseline = BASELINE_ENTITIES.get(register_name)
        source_writeable = bool(register.writeable)
        structured = class_name == "HUAWEI_LUNA2000_TimeOfUseRegisters"
        unit = UNIT_OVERRIDES.get(register_name, register.unit)
        if isinstance(unit, str):
            unit = UNIT_NORMALIZATION.get(unit, unit)
        value_kind = _value_kind(class_name, unit, register_name)
        options = _enum_options(unit) if value_kind == "enum" else ()
        flags = _flag_options(unit) if value_kind == "intflag" else ()

        policy = WRITE_ALLOWLIST.get(register_name) if source_writeable else None
        if policy is not None:
            platform = policy.platform
        else:
            platform = "sensor"
        writeable = policy is not None

        unit_text = unit if isinstance(unit, str) else None
        device_class, state_class = _ha_classes(register_name, unit_text)
        timestamp_format = None
        if value_kind == "timestamp":
            device_class, state_class, unit_text = "timestamp", None, None
            timestamp_format = (
                "timestamp_utc"
                if register_name in UTC_TIMESTAMP_REGISTERS
                else "timestamp_local"
            )
        if value_kind in ("ipv4", "intflag", "string", "enum"):
            state_class = None
            if value_kind != "enum":
                unit_text = None
                device_class = None

        client_role = register_client_role(register_name)
        device_role = register_device_role(register_name)
        poll_group = _poll_group(register_name, class_name, source_writeable)
        if register_name in SMARTGUARD_POLL_GROUPS:
            poll_group = SMARTGUARD_POLL_GROUPS[register_name]
        # Home Assistant only permits diagnostic entity categories for sensors.
        # TOU remains writable through the integration service, while its sensor
        # is the diagnostic readback of the configured schedule.
        category = (
            "diagnostic"
            if structured or (source_writeable and not writeable)
            else "config"
            if writeable
            else _entity_category(register_name, class_name)
        )
        if register_name == str(rn.SMARTGUARD_STATUS):
            category = None
        minimum = maximum = step = None
        if writeable and register_name in SAFE_NUMBER_RANGES:
            minimum, maximum, step = SAFE_NUMBER_RANGES[register_name]

        name = FRIENDLY_NAME_OVERRIDES.get(register_name, _friendly_name(register_name))
        if baseline is not None:
            # Keep 0.14.9 names, poll groups and default enablement stable so
            # upgrading never renames or re-enables existing entities.
            baseline_platform, enabled_default, _, _, poll_group, name = baseline
            if register_name == str(rn.EMMA_TOU_PERIODS):
                # BESS Manager reads the native schedule from this sensor's
                # numbered attributes; it is the readback contract and is
                # therefore enabled by default from 0.15.0.
                enabled_default = True
            if register_name in DEMOTED_LEGACY_CONTROLS:
                enabled_default = False
        elif client_role == "smartguard":
            # Explicit policy exception: every readable native SmartGuard
            # register is enabled and subscribed by default. Home Assistant
            # only creates them enabled once a SmartGuard is discovered.
            enabled_default = True
        else:
            # Newly introduced optional registers stay disabled by default.
            enabled_default = False

        depends_on: tuple[str, ...] = ()
        if timestamp_format == "timestamp_local":
            depends_on = tuple(
                ref
                for ref in LOCAL_TIMESTAMP_REFERENCES.get(
                    register_name, _SITE_OFFSET_REFERENCE
                )
                if ref != register_name
            )

        catalog[register_name] = EntityDescription(
            register_name=register_name,
            name=name,
            address=register.register,
            length=register.length,
            platform=platform,
            poll_group=poll_group,
            device_role=device_role,
            client_role=client_role,
            unit=unit_text,
            device_class=device_class,
            state_class=state_class,
            entity_category=category,
            icon=_icon(register_name, device_class, structured),
            enabled_default=enabled_default,
            writeable=writeable,
            options=options,
            minimum=minimum,
            maximum=maximum,
            step=step,
            format="tou_periods" if structured else timestamp_format,
            value_kind=value_kind,
            allowed_values=policy.allowed_values if policy else (),
            required_capabilities=policy.required_capabilities if policy else (),
            depends_on=depends_on,
            three_phase_only=register_name in SMARTGUARD_THREE_PHASE_REGISTERS,
            flags=flags,
        )
    return catalog


def register_device_role(register_name: str) -> str:
    """Map a register to the physical Home Assistant device that owns the data."""
    definition = REGISTERS[register_name]
    if TargetDevice.EMMA not in definition.target_device:
        return _target_client_role(definition.target_device)
    if register_name.startswith("emma_external_meter_") or "external_energy" in register_name:
        return "external_meter"
    # EMMA aggregate backup telemetry stays associated with the SmartGuard
    # device as in 0.14.x, but is still read from EMMA unit 0.
    if "backup_power" in register_name or "smartguard" in register_name:
        return "smartguard"
    if register_name.startswith("inverter_"):
        return "inverter"
    if register_name in SUN2000_EXPOSED_REGISTERS:
        return "inverter"
    return "emma"


def register_client_role(register_name: str) -> str:
    """Return the Modbus endpoint (device role) that owns a register.

    EMMA's ``inverter_*`` registers at 30302-30364 are aggregate values exposed
    by EMMA unit 0 even though Home Assistant groups them under the inverter
    device. Native downstream registers are read from the unit ID of the
    discovered device of that type.
    """
    definition = REGISTERS[register_name]
    if TargetDevice.EMMA in definition.target_device:
        return "emma"
    return _target_client_role(definition.target_device)


def _target_client_role(targets: TargetDevice) -> str:
    """Choose the Modbus unit role for a non-EMMA register definition.

    There is deliberately no EMMA fallback: an unknown target would otherwise
    be read from EMMA unit 0, where the same address usually holds a
    different register.
    """
    for target, role in _TARGET_ROLES:
        if target in targets:
            return role
    raise ValueError(f"Register target device {targets!r} has no connector role")


def _value_kind(class_name: str, unit: Any, register_name: str) -> str:
    if class_name == "TimestampRegister":
        return "timestamp"
    if class_name == "IPv4Register":
        return "ipv4"
    if class_name == "StringRegister":
        return "string"
    if "Period" in class_name or "TimeOfUse" in class_name:
        return "structured"
    if unit is bool:
        return "bool"
    if isinstance(unit, type) and issubclass(unit, IntFlag):
        return "intflag"
    if isinstance(unit, type) and issubclass(unit, IntEnum):
        return "enum"
    if isinstance(unit, dict):
        return "map"
    if callable(unit) and not isinstance(unit, type):
        return "bitfield"
    return "number"


def _flag_options(unit: Any) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"bit": int(member.value).bit_length() - 1, "key": str(member.name).lower()}
        for member in unit
        if member.value and member.name
    )


def timestamp_utc_offset(values: dict[str, Any], register_name: str) -> int | None:
    """Return the UTC offset in seconds for a local wall-clock timestamp register.

    The offset is derived from the device's own local and UTC clock registers
    and rounded to 15 minutes, so it follows the device's own daylight saving
    time handling (for example Europe/Helsinki UTC+2/UTC+3).
    """
    local_name, utc_name = LOCAL_TIMESTAMP_REFERENCES.get(
        register_name, _SITE_OFFSET_REFERENCE
    )
    local, utc = values.get(local_name), values.get(utc_name)
    if not isinstance(local, (int, float)) or not isinstance(utc, (int, float)):
        return None
    if isinstance(local, bool) or isinstance(utc, bool):
        return None
    offset = round((float(local) - float(utc)) / 900) * 900
    return int(offset) if abs(offset) <= 14 * 3600 else None


def timestamp_epoch(
    register_name: str, values: dict[str, Any], timestamp_format: str | None
) -> int | None:
    """Normalize a raw timestamp register value to a UTC epoch.

    Connector APIs keep publishing the raw register seconds for backward
    compatibility. UTC registers are returned unchanged. Local wall-clock
    registers are shifted by the device's derived UTC offset and are ``None``
    when that offset is not yet known, so a wrong time is never shown.
    """
    raw = values.get(register_name)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if timestamp_format != "timestamp_local":
        return int(raw)
    offset = timestamp_utc_offset(values, register_name)
    return None if offset is None else int(raw) - offset


def catalog_sha256(catalog: dict[str, EntityDescription]) -> str:
    """Return a stable hash of the physical entity catalog (all runtimes)."""
    payload = [catalog[name].to_dict() for name in sorted(catalog)]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(canonical.encode()).hexdigest()


def grouped_register_names(
    catalog: dict[str, EntityDescription],
) -> dict[str, list[str]]:
    result = {name: [] for name in POLL_INTERVALS}
    for register_name, description in catalog.items():
        result[description.poll_group].append(register_name)
    for names in result.values():
        names.sort(key=lambda item: catalog[item].address)
    return result


def json_value(value: Any) -> Any:
    """Convert a decoded huawei-solar value to the connector JSON contract.

    The representation is identical in every runtime: enums use their
    lower-case member name, IntFlag feature masks their raw integer, IPv4
    addresses dotted notation and timestamps the raw register seconds (as in
    0.14.x; see :func:`timestamp_epoch` for UTC normalization).
    """
    if isinstance(value, IntFlag):
        return int(value)
    if isinstance(value, IntEnum):
        return value.name.lower()
    if isinstance(value, datetime):
        # TimestampRegister decodes the raw seconds as a UTC datetime.
        return int(value.timestamp())
    if isinstance(value, IPv4Address):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if is_dataclass(value):
        return {key: json_value(item) for key, item in asdict(value).items()}
    if hasattr(value, "__dict__"):
        return {key: json_value(item) for key, item in vars(value).items()}
    return str(value)


def _enum_options(unit: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(unit, type) or not issubclass(unit, IntEnum):
        return ()
    options = []
    for member in unit:
        label = _friendly_name(member.name.lower())
        if unit.__name__ == "StorageExcessPvEnergyUseInTOU":
            label = "Battery First" if member.name == "CHARGE" else "Appliances First"
        options.append({
            "value": int(member.value),
            "key": member.name.lower(),
            "label": label,
        })
    return tuple(options)


def _poll_group(name: str, class_name: str, writeable: bool) -> str:
    if "String" in class_name or any(
        word in name
        for word in (
            "model",
            "serial",
            "software_version",
            "firmware_version",
            "protocol_version",
            "product_",
            "rated_",
            "feature_mask",
            "support_flag",
            "nb_",
            "number_of_",
        )
    ):
        return "slow"
    if writeable or any(
        word in name
        for word in (
            "energy",
            "electricity",
            "yield",
            "consumption",
            "capacity",
            "period",
            "mode",
            "limitation",
            "number_of",
            "time",
            "alarm",
            "status",
            "temperature",
        )
    ):
        return "medium"
    return "fast"


def _entity_category(name: str, class_name: str) -> str | None:
    if "String" in class_name or any(
        word in name
        for word in (
            "model",
            "serial",
            "software",
            "version",
            "status",
            "alarm",
            "fault",
            "feature_mask",
            "support_flag",
            "product_",
            "number_of_",
            "nb_",
        )
    ):
        return "diagnostic"
    return None


def _ha_classes(name: str, unit: str | None) -> tuple[str | None, str | None]:
    if unit in ("W", "kW"):
        return "power", "measurement"
    if unit in ("VA", "kVA"):
        return "apparent_power", "measurement"
    if unit == "V":
        return "voltage", "measurement"
    if unit == "A":
        return "current", "measurement"
    if unit == "Hz":
        return "frequency", "measurement"
    if unit in ("°C", "℃"):
        return "temperature", "measurement"
    if unit == "%":
        return ("battery" if "state_of" in name else None), "measurement"
    if unit in ("Wh", "kWh", "MWh"):
        if any(word in name for word in ("capacity", "chargeable", "dischargeable")):
            return "energy_storage", "measurement"
        state = "total_increasing" if "total" in name else "total"
        return "energy", state
    return None, "measurement" if unit is not None else None


def _icon(name: str, device_class: str | None, structured: bool) -> str | None:
    if structured or "tou" in name:
        return "mdi:calendar-clock"
    if "pv" in name or "solar" in name:
        return "mdi:solar-power"
    if "battery" in name or "ess" in name or "storage" in name:
        return "mdi:battery"
    if "grid" in name or "feed_in" in name:
        return "mdi:transmission-tower"
    if "load" in name or "consumption" in name:
        return "mdi:home-lightning-bolt"
    if "model" in name or "serial" in name or "software" in name or "firmware" in name:
        return "mdi:information-outline"
    if "alarm" in name or "fault" in name:
        return "mdi:alert-circle-outline"
    if "status" in name or "state" in name:
        return "mdi:list-status"
    if "temperature" in name:
        return "mdi:thermometer"
    if "frequency" in name:
        return "mdi:sine-wave"
    if "current" in name:
        return "mdi:current-ac"
    if "voltage" in name:
        return "mdi:flash-triangle-outline"
    if "power_factor" in name:
        return "mdi:angle-acute"
    if "optimizer" in name:
        return "mdi:solar-panel"
    if "charger" in name:
        return "mdi:ev-station"
    if "meter" in name:
        return "mdi:meter-electric-outline"
    if "time" in name or "date" in name:
        return "mdi:clock-outline"
    if device_class:
        return None
    return "mdi:chip"


def _friendly_name(value: str) -> str:
    # Huawei's register identifiers use BUILT_IN_ENERGY and EXTERNAL_ENERGY as
    # shorthand for the built-in/external *energy meter*. They contain live
    # voltage, current, power and power-factor values as well as energy totals.
    # Put the meter source first so a voltage is not mislabeled as energy.
    for suffix, source in (
        ("_built_in_energy", "Built-in Meter"),
        ("_external_energy", "External Meter"),
    ):
        if value.endswith(suffix):
            measurement = value[: -len(suffix)]
            return f"{source} {_friendly_name(measurement)}"

    replacements = {
        "Pv": "PV",
        "Ess": "ESS",
        "Tou": "TOU",
        "Emma": "EMMA",
        "Soc": "SOC",
        "Soh": "SOH",
        "Soe": "SOE",
        "Mpp": "MPP",
        "Pn": "PN",
        "Ac": "AC",
        "Dc": "DC",
    }
    text = value.replace("_", " ").title()
    for original, replacement in replacements.items():
        # Acronyms are words, not arbitrary substrings: replacing ``Ac`` in
        # ``Active`` previously produced the visible typo ``ACtive``.
        text = re.sub(rf"\b{re.escape(original)}\b", replacement, text)
    # Phase-to-phase voltage identifiers use A_B/B_C/C_A; render the pair as
    # the conventional electrical notation A-B/B-C/C-A.
    text = re.sub(r"\b([ABC]) ([ABC])\b", r"\1-\2", text)
    return text
