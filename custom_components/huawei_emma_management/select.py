from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, TOU_MAX_PERIODS
from .entity import EmmaEntity
from .tou import editor_description


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        EmmaSelect(coordinator, description)
        for description in coordinator.entity_descriptions
        if description["platform"] == "select"
    ]
    entities.extend([TouPeriodSelect(coordinator), TouPeriodModeSelect(coordinator)])
    async_add_entities(entities)


class EmmaSelect(EmmaEntity, SelectEntity):
    def __init__(self, coordinator: Any, description: dict[str, Any]) -> None:
        super().__init__(coordinator, description)
        self._key_to_label = {
            option["key"]: option["label"] for option in description["options"]
        }
        self._label_to_key = {label: key for key, label in self._key_to_label.items()}
        self._attr_options = list(self._label_to_key)

    @property
    def current_option(self) -> str | None:
        value = self.register_value
        return self._key_to_label.get(str(value), str(value)) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_value(
            self.description["register_name"],
            self._label_to_key[option],
            source=f"home_assistant_select(label={option!r})",
        )


class TouPeriodSelect(EmmaEntity, SelectEntity):
    _attr_options = [str(index) for index in range(1, TOU_MAX_PERIODS + 1)]

    def __init__(self, coordinator: Any) -> None:
        super().__init__(
            coordinator,
            editor_description("selected_period"),
        )

    @property
    def current_option(self) -> str:
        return str(self.coordinator.selected_tou_slot + 1)

    async def async_select_option(self, option: str) -> None:
        self.coordinator.select_tou_slot(int(option) - 1)


class TouPeriodModeSelect(EmmaEntity, SelectEntity):
    _attr_options = ["Charge", "Discharge"]

    def __init__(self, coordinator: Any) -> None:
        super().__init__(
            coordinator,
            editor_description("mode"),
        )

    @property
    def current_option(self) -> str:
        slot = self.coordinator.tou_slots[self.coordinator.selected_tou_slot]
        return str(slot["mode"]).title()

    async def async_select_option(self, option: str) -> None:
        self.coordinator.update_selected_tou_slot(mode=option.lower())
