"""Huawei EMMA register metadata shared by embedded and external connectors."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import IntEnum
from typing import Any

from huawei_solar import register_names as rn
from huawei_solar.register_definitions import TargetDevice
from huawei_solar.registers import REGISTERS


POLL_INTERVALS = {"fast": 30, "medium": 300, "slow": 1800}

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


@dataclass(frozen=True, slots=True)
class EntityDescription:
    register_name: str
    name: str
    address: int
    length: int
    platform: str
    poll_group: str
    device_role: str = "emma"
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def build_entity_catalog() -> dict[str, EntityDescription]:
    catalog: dict[str, EntityDescription] = {}
    for key, register in REGISTERS.items():
        is_emma = TargetDevice.EMMA in register.target_device
        is_exposed_sun2000 = (
            TargetDevice.SUN2000 in register.target_device
            and str(key) in SUN2000_EXPOSED_REGISTERS
        )
        if not register.readable or not (is_emma or is_exposed_sun2000):
            continue
        register_name = str(key)
        writeable = bool(register.writeable)
        structured = register.__class__.__name__ == "HUAWEI_LUNA2000_TimeOfUseRegisters"
        unit = UNIT_OVERRIDES.get(register_name, register.unit)
        options = _enum_options(unit)

        if register_name == str(rn.EMMA_SYSTEM_TIME):
            platform = "datetime"
        elif structured:
            platform = "sensor"
        elif writeable and options:
            platform = "select"
        elif writeable and unit is bool:
            platform = "switch"
        elif writeable and register_name in SAFE_NUMBER_RANGES:
            platform = "number"
        else:
            platform = "sensor"

        unit_text = unit if isinstance(unit, str) else None
        device_class, state_class = _ha_classes(register_name, unit_text)
        if register_name in (str(rn.EMMA_SYSTEM_TIME), str(rn.EMMA_LOCAL_TIME)):
            device_class, state_class, unit_text = "timestamp", None, None
        poll_group = _poll_group(register_name, register.__class__.__name__, writeable)
        # Home Assistant only permits diagnostic entity categories for sensors.
        # TOU remains writable through the integration service, while its sensor
        # is the diagnostic readback of the configured schedule.
        category = (
            "diagnostic"
            if structured
            else "config"
            if writeable
            else _entity_category(register_name, register.__class__.__name__)
        )
        minimum = maximum = step = None
        if register_name in SAFE_NUMBER_RANGES:
            minimum, maximum, step = SAFE_NUMBER_RANGES[register_name]

        catalog[register_name] = EntityDescription(
            register_name=register_name,
            name=FRIENDLY_NAME_OVERRIDES.get(register_name, _friendly_name(register_name)),
            address=register.register,
            length=register.length,
            platform=platform,
            poll_group=poll_group,
            device_role=register_device_role(register_name),
            unit=unit_text,
            device_class=device_class,
            state_class=state_class,
            entity_category=category,
            icon=_icon(register_name, device_class, structured),
            enabled_default=(
                False
                if structured
                else _enabled_default(register_name, poll_group, writeable)
            ),
            writeable=writeable,
            options=options,
            minimum=minimum,
            maximum=maximum,
            step=step,
            format="tou_periods" if structured else None,
        )
    return catalog


def register_device_role(register_name: str) -> str:
    """Map EMMA aggregate registers to the physical device that owns the data."""
    if register_name.startswith("emma_external_meter_"):
        return "external_meter"
    if "backup_power" in register_name or "smartguard" in register_name:
        return "smartguard"
    if register_name.startswith("inverter_"):
        return "inverter"
    if register_name in SUN2000_EXPOSED_REGISTERS:
        return "inverter"
    return "emma"


def register_client_role(register_name: str) -> str:
    """Return the Modbus endpoint that owns a register.

    EMMA's ``inverter_*`` registers at 30302-30364 are aggregate values exposed
    by EMMA unit 0 even though Home Assistant groups them under the inverter
    device. Only the explicitly exposed native SUN2000 controls are read from
    the inverter's unit ID.
    """
    return "inverter" if register_name in SUN2000_EXPOSED_REGISTERS else "emma"


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
    if isinstance(value, IntEnum):
        return value.name.lower()
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
        word in name for word in ("model", "serial", "software_version")
    ):
        return "slow"
    if writeable or any(
        word in name
        for word in (
            "energy",
            "yield",
            "consumption",
            "capacity",
            "period",
            "mode",
            "limitation",
            "number_of",
            "time",
        )
    ):
        return "medium"
    return "fast"


def _entity_category(name: str, class_name: str) -> str | None:
    if "String" in class_name or any(
        word in name
        for word in ("model", "serial", "software", "version", "status", "alarm")
    ):
        return "diagnostic"
    return None


def _enabled_default(name: str, poll_group: str, writeable: bool) -> bool:
    if name.startswith("emma_external_meter_"):
        return False
    if "built_in_energy" in name:
        return True
    if writeable:
        return True
    if poll_group == "slow":
        return True
    advanced = (
        "phase_a_",
        "phase_b_",
        "phase_c_",
        "line_voltage_",
        "apparent_power",
        "power_factor",
    )
    return not any(part in name for part in advanced)


def _ha_classes(name: str, unit: str | None) -> tuple[str | None, str | None]:
    if unit == "W":
        return "power", "measurement"
    if unit == "VA":
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
    if unit == "kWh":
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
    if "model" in name or "serial" in name or "software" in name:
        return "mdi:information-outline"
    if device_class:
        return None
    return "mdi:chip"


def _friendly_name(value: str) -> str:
    replacements = {"Pv": "PV", "Ess": "ESS", "Tou": "TOU", "Emma": "EMMA"}
    text = value.replace("_", " ").title()
    for original, replacement in replacements.items():
        text = text.replace(original, replacement)
    return text
