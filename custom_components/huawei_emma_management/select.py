from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
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
        # Connectors from 0.15 send the reviewed raw values that may be written;
        # older connectors send none, in which case every option is offered.
        allowed = set(description.get("allowed_values") or ())
        self._key_to_label = {
            option["key"]: option["label"]
            for option in description["options"]
            if not allowed or option.get("value") in allowed
        }
        self._all_labels = {
            option["key"]: option["label"] for option in description["options"]
        }
        self._label_to_key = {label: key for key, label in self._key_to_label.items()}

    @property
    def options(self) -> list[str]:
        """Writable options, plus the current device value when outside them."""
        options = list(self._label_to_key)
        current = self.current_option
        if current is not None and current not in options:
            options.append(current)
        return options

    @property
    def current_option(self) -> str | None:
        value = self.register_value
        if value is None:
            return None
        return self._key_to_label.get(str(value)) or self._all_labels.get(
            str(value), str(value)
        )

    async def async_select_option(self, option: str) -> None:
        if option not in self._label_to_key:
            raise ServiceValidationError(
                f"{option} is not a reviewed writable value for "
                f"{self.description['register_name']}"
            )
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
