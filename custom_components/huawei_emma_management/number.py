from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EmmaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EmmaNumber(coordinator, description)
        for description in coordinator.entity_descriptions
        if description["platform"] == "number"
    )


class EmmaNumber(EmmaEntity, NumberEntity):
    def __init__(self, coordinator: Any, description: dict[str, Any]) -> None:
        super().__init__(coordinator, description)
        self._attr_native_min_value = description["minimum"]
        self._attr_native_max_value = description["maximum"]
        self._attr_native_step = description["step"]
        self._attr_native_unit_of_measurement = description.get("unit")
        self._attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> float | None:
        value = self.register_value
        if value is None:
            return None
        return int(value) if self.description.get("step") == 1 else float(value)

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_value(
            self.description["register_name"],
            value,
            source="home_assistant_number",
        )
