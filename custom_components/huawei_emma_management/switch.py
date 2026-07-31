from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    entities = [
        EmmaSwitch(coordinator, description)
        for description in coordinator.entity_descriptions
        if description["platform"] == "switch"
    ]
    entities.append(TouPeriodEnabledSwitch(coordinator))
    async_add_entities(entities)


class EmmaSwitch(EmmaEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        value = self.register_value
        return bool(value) if value is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_value(
            self.description["register_name"],
            True,
            source="home_assistant_switch",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_value(
            self.description["register_name"],
            False,
            source="home_assistant_switch",
        )


class TouPeriodEnabledSwitch(EmmaEntity, SwitchEntity):
    def __init__(self, coordinator: Any) -> None:
        super().__init__(
            coordinator,
            editor_description("enabled"),
        )

    @property
    def is_on(self) -> bool:
        slot = self.coordinator.tou_slots[self.coordinator.selected_tou_slot]
        return bool(slot["enabled"])

    async def async_turn_on(self, **kwargs: Any) -> None:
        self.coordinator.update_selected_tou_slot(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self.coordinator.update_selected_tou_slot(enabled=False)
