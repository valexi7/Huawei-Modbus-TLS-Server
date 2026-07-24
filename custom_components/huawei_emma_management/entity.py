from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmmaCoordinator


class EmmaEntity(CoordinatorEntity[EmmaCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EmmaCoordinator, description: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self.description = description
        register_name = description["register_name"]
        serial = coordinator.unique_id_prefix
        self._attr_unique_id = f"{serial}_{register_name}"
        self._attr_name = description["name"]
        self._attr_icon = description.get("icon")
        category = description.get("entity_category")
        if category:
            self._attr_entity_category = EntityCategory(category)
        self._attr_entity_registry_enabled_default = description.get("enabled_default", True)

    @property
    def device_info(self) -> DeviceInfo:
        device = self.coordinator.device_data
        parent_serial = self.coordinator.parent_identifier
        role = self.description.get("device_role", "emma")
        matches = (
            []
            if role == "emma"
            else [
                item for item in device.get("devices", []) if item.get("role") == role
            ]
        )
        owner = matches[0] if len(matches) == 1 else device
        serial = (
            parent_serial
            if owner is device
            else owner.get("serial_number") or f"{parent_serial}:{role}"
        )
        return DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            manufacturer="Huawei",
            model=owner.get("model") or "EMMA",
            name=f"Huawei {owner.get('model') or 'EMMA'}",
            serial_number=owner.get("serial_number"),
            sw_version=owner.get("sw_version"),
            via_device=(DOMAIN, str(parent_serial)) if str(serial) != str(parent_serial) else None,
        )

    @property
    def available(self) -> bool:
        unsupported = self.coordinator.data.get("unsupported", []) if self.coordinator.data else []
        source_register = self.description.get(
            "source_register_name", self.description["register_name"]
        )
        return (
            super().available
            and bool(self.coordinator.data.get("health", {}).get("connected"))
            and source_register not in unsupported
            and source_register in self.coordinator.data.get("values", {})
        )

    @property
    def register_value(self) -> Any:
        source_register = self.description.get(
            "source_register_name", self.description["register_name"]
        )
        return self.coordinator.data.get("values", {}).get(source_register)
