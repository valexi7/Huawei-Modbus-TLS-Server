"""Config entry diagnostics for Huawei EMMA Management."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PRIVATE_KEY_PASSWORD, CONF_TOKEN, DOMAIN

TO_REDACT = {CONF_TOKEN, CONF_PRIVATE_KEY_PASSWORD, "serial_number", "sn"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator = hass.data[DOMAIN][entry.entry_id]
    diagnostics = getattr(coordinator.api, "diagnostics", None)
    connector = await diagnostics() if diagnostics is not None else {}
    return async_redact_data(
        {
            "entry": {
                "version": entry.version,
                "minor_version": entry.minor_version,
                "data": dict(entry.data),
                "options": dict(entry.options),
            },
            "connector": connector,
            "device_roles": sorted(
                {
                    str(item.get("role"))
                    for item in coordinator.device_data.get("devices", [])
                    if isinstance(item, dict)
                }
            ),
            "accepted_subscriptions": sorted(
                coordinator._accepted_subscriptions or []
            ),
        },
        TO_REDACT,
    )
