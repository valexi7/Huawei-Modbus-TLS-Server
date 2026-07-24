from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.datetime import DateTimeEntity
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
        EmmaDateTime(coordinator, description)
        for description in coordinator.entity_descriptions
        if description["platform"] == "datetime"
    )


class EmmaDateTime(EmmaEntity, DateTimeEntity):
    @property
    def native_value(self) -> datetime | None:
        value = self.register_value
        if value is None:
            return None
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    async def async_set_value(self, value: datetime) -> None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        await self.coordinator.async_set_value(
            self.description["register_name"],
            round(value.timestamp()),
            source=f"home_assistant_datetime(iso={value.isoformat()})",
        )
