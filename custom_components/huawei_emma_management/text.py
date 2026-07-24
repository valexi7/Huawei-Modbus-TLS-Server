from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import EmmaEntity
from .tou import decode_tou_plan_json, editor_description, encode_tou_plan_json


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TouPlanJsonText(coordinator)])


class TouPlanJsonText(EmmaEntity, TextEntity):
    _attr_mode = TextMode.TEXT
    _attr_native_min = 2
    _attr_native_max = 255

    def __init__(self, coordinator: Any) -> None:
        description = editor_description(
            "plan_json",
            "TOU 10. Plan JSON",
            "mdi:code-json",
        )
        description["entity_category"] = "diagnostic"
        super().__init__(coordinator, description)

    @property
    def native_value(self) -> str:
        return encode_tou_plan_json(self.coordinator.planned_tou_periods())

    async def async_set_value(self, value: str) -> None:
        _LOGGER.debug("TOU JSON input received value=%r", value)
        try:
            periods = decode_tou_plan_json(value)
        except ValueError as error:
            _LOGGER.debug("TOU JSON input rejected reason=%s value=%r", error, value)
            raise
        _LOGGER.debug("TOU JSON input validation=accepted periods=%r", periods)
        self.coordinator.replace_tou_plan(periods)
