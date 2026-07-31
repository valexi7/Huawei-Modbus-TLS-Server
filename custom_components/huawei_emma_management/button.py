from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
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
        [TouClearPeriodButton(coordinator), TouApplyButton(coordinator)]
    )


class TouApplyButton(EmmaEntity, ButtonEntity):
    def __init__(self, coordinator: Any) -> None:
        super().__init__(
            coordinator,
            editor_description("apply_schedule"),
        )

    async def async_press(self) -> None:
        await self.coordinator.async_apply_tou_schedule()


class TouClearPeriodButton(EmmaEntity, ButtonEntity):
    def __init__(self, coordinator: Any) -> None:
        super().__init__(
            coordinator,
            editor_description("clear_period"),
        )

    async def async_press(self) -> None:
        self.coordinator.clear_selected_tou_slot()
