from __future__ import annotations

from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EmmaEntity
from .tou import editor_description


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            TouPeriodTime(coordinator, "start_time"),
            TouPeriodTime(coordinator, "end_time"),
        ]
    )


class TouPeriodTime(EmmaEntity, TimeEntity):
    def __init__(self, coordinator: Any, field: str) -> None:
        super().__init__(
            coordinator,
            editor_description(field),
        )
        self.field = field

    @property
    def native_value(self) -> time:
        slot = self.coordinator.tou_slots[self.coordinator.selected_tou_slot]
        minutes = int(slot[self.field])
        minutes = max(0, min(1439, minutes))
        hour, minute = divmod(minutes, 60)
        return time(hour=hour, minute=minute)

    async def async_set_value(self, value: time) -> None:
        self.coordinator.update_selected_tou_slot(
            **{self.field: value.hour * 60 + value.minute}
        )
