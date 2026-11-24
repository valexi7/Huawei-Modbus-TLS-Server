from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .embedded_catalog import (
    DERIVED_ENTITY_DESCRIPTIONS,
    VIRTUAL_ENTITY_DESCRIPTIONS,
    derived_power_value,
    timestamp_epoch,
)
from .entity import EmmaEntity
from .tou import format_tou_schedule, tou_period_attributes


TOU_ACTIVE_DESCRIPTION = VIRTUAL_ENTITY_DESCRIPTIONS["emma_tou_schedule_text"]
TOU_PLANNED_DESCRIPTION = VIRTUAL_ENTITY_DESCRIPTIONS[
    "emma_tou_planned_schedule_text"
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        EmmaSensor(coordinator, description)
        for description in coordinator.entity_descriptions
        if description["platform"] == "sensor"
    ]
    entities.extend(
        [
            EmmaSensor(coordinator, TOU_ACTIVE_DESCRIPTION),
            EmmaSensor(coordinator, TOU_PLANNED_DESCRIPTION),
        ]
    )
    entities.extend(
        EmmaDerivedPowerSensor(coordinator, description)
        for description in DERIVED_ENTITY_DESCRIPTIONS.values()
        if description["platform"] == "sensor"
    )
    async_add_entities(entities)


class EmmaSensor(EmmaEntity, SensorEntity):
    def __init__(self, coordinator: Any, description: dict[str, Any]) -> None:
        super().__init__(coordinator, description)
        self._attr_native_unit_of_measurement = description.get("unit")
        if description.get("device_class"):
            self._attr_device_class = SensorDeviceClass(description["device_class"])
        if description.get("state_class"):
            self._attr_state_class = SensorStateClass(description["state_class"])

    @property
    def native_value(self) -> Any:
        value = self.register_value
        if self.description.get("format") == "tou_periods":
            return len(value) if isinstance(value, list) else None
        if self.description.get("format") in (
            "tou_active_schedule_text",
            "tou_planned_schedule_text",
        ):
            periods = (
                self.coordinator.planned_tou_periods()
                if self.description.get("format") == "tou_planned_schedule_text"
                else value
            )
            schedule, lines = format_tou_schedule(periods)
            return (
                schedule
                if len(schedule) <= 255
                else f"{len(lines)} periods; see schedule_text attribute"
            )
        if self.description.get("device_class") == "timestamp" and value is not None:
            epoch = timestamp_epoch(
                self.description["register_name"],
                self.coordinator.data.get("values", {}),
                # Connectors older than 0.15.0 do not publish the timestamp
                # format; their two timestamp registers were treated as UTC.
                self.description.get("format") or "timestamp_utc",
            )
            return (
                datetime.fromtimestamp(epoch, tz=timezone.utc)
                if epoch is not None
                else None
            )
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.description.get("format") == "tou_periods":
            # "Period N" attributes use the LUNA text format read by BESS
            # Manager (same contract as huawei_solar's TOU sensor). The sensor
            # is unavailable, never an empty schedule, when no readback exists.
            periods = self.register_value
            return {
                "periods": periods,
                **tou_period_attributes(periods),
                "updated_at": self.coordinator.data.get("updated_at", {}).get(
                    self.description["register_name"]
                ),
                "register_address": self.description["address"],
            }
        if self.description.get("format") in (
            "tou_active_schedule_text",
            "tou_planned_schedule_text",
        ):
            periods = (
                self.coordinator.planned_tou_periods()
                if self.description.get("format") == "tou_planned_schedule_text"
                else self.register_value
            )
            schedule, lines = format_tou_schedule(periods)
            attributes = {
                "schedule_text": schedule,
                "schedule_lines": lines,
                "periods": periods,
                "modified": self.coordinator.tou_dirty,
            }
            if self.description.get("format") == "tou_active_schedule_text":
                attributes.update(tou_period_attributes(periods))
            return attributes
        flags = self.description.get("flags")
        if flags and isinstance(self.register_value, int):
            return {
                "register_address": self.description["address"],
                "register_count": self.description.get("length", 1),
                "modbus_device_role": self.description.get("client_role", "emma"),
                "poll_group": self.description["poll_group"],
                "active_flags": [
                    flag["key"]
                    for flag in flags
                    if self.register_value & (1 << int(flag["bit"]))
                ],
            }
        return {
            "register_address": self.description["address"],
            "register_count": self.description.get("length", 1),
            "modbus_device_role": self.description.get("client_role", "emma"),
            "poll_group": self.description["poll_group"],
        }


class EmmaDerivedPowerSensor(EmmaEntity, SensorEntity):
    """Directional power derived from an already polled signed register.

    No additional Modbus request is made: the value is computed from the
    source register in the latest connector state, and the sensor is
    unavailable whenever the source value is unavailable or stale.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "W"

    @property
    def native_value(self) -> float | None:
        return derived_power_value(self.description, self.register_value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "source_register": self.description["source_register_name"],
            "source_polarity": self.description["source_polarity"],
            "derived": True,
        }
