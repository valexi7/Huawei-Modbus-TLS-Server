from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, READ_ONLY_ENTITY_KEY
from .embedded_catalog import (
    SMARTGUARD_BINARY_DESCRIPTIONS,
    SMARTGUARD_STATUS_CATEGORIES,
    smartguard_status_flag,
)
from .entity import EmmaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            *(
                SmartGuardStatusBinarySensor(coordinator, description)
                for description in SMARTGUARD_BINARY_DESCRIPTIONS.values()
            ),
            ReadOnlyModeBinarySensor(coordinator),
        ]
    )


class SmartGuardStatusBinarySensor(EmmaEntity, BinarySensorEntity):
    """Unambiguous grid/backup interpretation of the SmartGuard status.

    The state is unknown for transitional, bypassed and fault statuses, and
    the entity is unavailable whenever the SmartGuard status itself is
    unavailable or stale. It never reports an outage from missing data.
    """

    def __init__(self, coordinator: Any, description: dict[str, Any]) -> None:
        super().__init__(coordinator, description)
        if description.get("device_class"):
            self._attr_device_class = BinarySensorDeviceClass(description["device_class"])

    @property
    def is_on(self) -> bool | None:
        return smartguard_status_flag(self.description["register_name"], self.register_value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.register_value
        return {
            "smartguard_status": status,
            "status_category": SMARTGUARD_STATUS_CATEGORIES.get(status)
            if isinstance(status, str)
            else None,
        }


READ_ONLY_DESCRIPTION: dict[str, Any] = {
    "register_name": READ_ONLY_ENTITY_KEY,
    "name": "Read-Only Mode",
    "platform": "binary_sensor",
    "device_role": "emma",
    "client_role": "emma",
    "entity_category": "diagnostic",
    "icon": "mdi:lock",
    "enabled_default": True,
}


class ReadOnlyModeBinarySensor(EmmaEntity, BinarySensorEntity):
    """On when EMMA read-only mode blocks every control action.

    Automations and BESS Manager integrations can use it to see that battery
    control is unavailable while telemetry continues.
    """

    def __init__(self, coordinator: Any) -> None:
        super().__init__(coordinator, dict(READ_ONLY_DESCRIPTION))

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        return self.coordinator.read_only

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "control_available": not self.coordinator.read_only,
            "integration_option": self.coordinator.read_only_option,
            "connector_reported": self.coordinator.connector_read_only,
        }
