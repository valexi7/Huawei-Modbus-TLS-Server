from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EmmaEntity
from .tou import format_tou_schedule


TOU_ACTIVE_DESCRIPTION = {
    "register_name": "emma_tou_schedule_text",
    "source_register_name": "emma_tou_periods",
    "name": "TOU 1. Active Schedule",
    "platform": "sensor",
    "poll_group": "medium",
    "device_role": "emma",
    "entity_category": "diagnostic",
    "icon": "mdi:calendar-clock",
    "enabled_default": True,
    "address": 40004,
    "format": "tou_active_schedule_text",
}

TOU_PLANNED_DESCRIPTION = {
    "register_name": "emma_tou_planned_schedule_text",
    "source_register_name": "emma_tou_periods",
    "name": "TOU 2. Planned Schedule",
    "platform": "sensor",
    "poll_group": "medium",
    "device_role": "emma",
    "entity_category": "diagnostic",
    "icon": "mdi:calendar-edit",
    "enabled_default": True,
    "address": 40004,
    "format": "tou_planned_schedule_text",
}


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
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.description.get("format") == "tou_periods":
            return {"periods": self.register_value}
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
            return {
                "schedule_text": schedule,
                "schedule_lines": lines,
                "periods": periods,
                "modified": self.coordinator.tou_dirty,
            }
        return {
            "register_address": self.description["address"],
            "poll_group": self.description["poll_group"],
        }
