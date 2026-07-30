from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EmbeddedEmmaApiClient, EmmaApiClient, EmmaApiError
from .const import (
    CERTIFICATE_AUTOMATIC,
    CONF_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
    CONF_CERTIFICATE_MODE,
    CONF_CERTIFICATE_NAME,
    CONF_CERTIFICATE_PATH,
    CONF_DISCOVERED_DEVICE,
    CONF_MODE,
    CONF_PRIVATE_KEY_PASSWORD,
    CONF_PRIVATE_KEY_PATH,
    CONF_TLS_PORT,
    CONF_TOKEN,
    DEFAULT_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
    DOMAIN,
    GROWATT_BATTERY_MODES,
    GROWATT_COMPAT_DOMAIN,
    GROWATT_MAX_SEGMENTS,
    HUAWEI_EXTERNAL_API_DOMAIN,
    PLATFORMS,
    TOU_REGISTER_NAME,
    DEFAULT_TLS_PORT,
    MODE_EMBEDDED,
    MODE_EXTERNAL,
)
from .coordinator import EmmaCoordinator
from .embedded_server import ReverseModbusTlsServer, ServerConfig
from .tou import decode_luna_tou_periods


_LOGGER = logging.getLogger(__name__)
_GROWATT_ALIAS_OWNER = f"{DOMAIN}_owns_growatt_aliases"
_HUAWEI_API_OWNER = f"{DOMAIN}_owns_huawei_api"
_SERVICE_NAMES = ("set_tou_periods", "read_time_segments", "update_time_segment")
_HUAWEI_API_SERVICE_NAMES = ("read_controls", "set_value", "set_tou_periods")
_SAFE_CONTROL_PLATFORMS = {"select", "switch", "number", "datetime"}


def _debug_schema(label: str, schema: vol.Schema):
    def validate(data: dict[str, Any]) -> dict[str, Any]:
        try:
            return schema(data)
        except vol.Invalid as error:
            _LOGGER.debug(
                "INPUT schema validation=rejected action=%s data=%r reason=%s",
                label,
                data,
                error,
            )
            raise

    return validate


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    mode = entry.data.get(CONF_MODE, MODE_EXTERNAL)
    embedded_server: ReverseModbusTlsServer | None = None
    if mode == MODE_EMBEDDED:
        storage_dir = Path(hass.config.path(DOMAIN, entry.entry_id, "certs"))
        if entry.data.get(CONF_CERTIFICATE_MODE, CERTIFICATE_AUTOMATIC) == CERTIFICATE_AUTOMATIC:
            certfile = storage_dir / "server-cert.pem"
            keyfile = storage_dir / "server-key.pem"
            ca_certfile = storage_dir / "ca-cert.pem"
            ca_keyfile = storage_dir / "ca-key.pem"
        else:
            certfile = _config_path(hass, entry.data[CONF_CERTIFICATE_PATH])
            keyfile = _config_path(hass, entry.data[CONF_PRIVATE_KEY_PATH])
            ca_certfile = storage_dir / "custom-certificate-ca-unused.pem"
            ca_keyfile = storage_dir / "custom-certificate-key-unused.pem"
        embedded_server = ReverseModbusTlsServer(
            ServerConfig(
                port=entry.data.get(CONF_TLS_PORT, DEFAULT_TLS_PORT),
                certfile=certfile,
                keyfile=keyfile,
                ca_certfile=ca_certfile,
                ca_keyfile=ca_keyfile,
                cert_name=entry.data.get(CONF_CERTIFICATE_NAME) or None,
                password=entry.data.get(CONF_PRIVATE_KEY_PASSWORD) or None,
                generate_certificates=entry.data.get(
                    CONF_CERTIFICATE_MODE, CERTIFICATE_AUTOMATIC
                )
                == CERTIFICATE_AUTOMATIC,
            )
        )
        try:
            await embedded_server.async_start()
        except (OSError, RuntimeError, ValueError) as error:
            raise ConfigEntryNotReady(
                f"Cannot start embedded EMMA TLS server: {error}"
            ) from error
        api = EmbeddedEmmaApiClient(embedded_server.state)
    else:
        api = EmmaApiClient(
            async_get_clientsession(hass),
            entry.data[CONF_HOST],
            entry.data[CONF_PORT],
            entry.data[CONF_TOKEN],
        )
    coordinator = EmmaCoordinator(
        hass,
        api,
        entry_id=entry.entry_id,
        initial_device_data=entry.data.get(CONF_DISCOVERED_DEVICE),
        stable_identifier=f"embedded:{entry.entry_id}"
        if mode == MODE_EMBEDDED
        else None,
    )
    coordinator.embedded_server = embedded_server
    coordinator.accept_external_growatt_controls = entry.options.get(
        CONF_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
        DEFAULT_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
    )
    _LOGGER.debug(
        "CONFIG entry=%s accept_external_growatt_controls=%s",
        entry.entry_id,
        coordinator.accept_external_growatt_controls,
    )
    try:
        await coordinator.async_initialize()
    except (EmmaApiError, UpdateFailed) as error:
        if embedded_server is not None:
            await embedded_server.async_stop()
        raise ConfigEntryNotReady(str(error)) from error
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entity_registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry, entry.entry_id
    ):
        unique_id = registry_entry.unique_id
        if (
            "_tou_period_" in unique_id
            or unique_id.endswith("_tou_clear_schedule")
        ):
            entity_registry.async_remove(registry_entry.entity_id)
            continue
        if (
            "_emma_external_meter_" in unique_id
            and registry_entry.disabled_by is None
        ):
            entity_registry.async_update_entity(
                registry_entry.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
        elif (
            "built_in_energy" in unique_id
            and registry_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
        ):
            entity_registry.async_update_entity(
                registry_entry.entity_id,
                disabled_by=None,
            )
        elif (
            unique_id.endswith("_emma_tou_periods")
            and registry_entry.disabled_by is None
        ):
            entity_registry.async_update_entity(
                registry_entry.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
    registry = dr.async_get(hass)
    parent_identifier = coordinator.parent_identifier
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, parent_identifier)},
        manufacturer="Huawei",
        model=coordinator.device_data.get("model") or "EMMA",
        name=f"Huawei {coordinator.device_data.get('model') or 'EMMA'}",
        serial_number=coordinator.device_data.get("serial_number"),
        sw_version=coordinator.device_data.get("sw_version"),
    )
    for device in coordinator.device_data.get("devices", []):
        serial = device.get("serial_number")
        if not serial or str(serial) == str(
            coordinator.device_data.get("serial_number")
        ):
            continue
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, str(serial))},
            manufacturer="Huawei",
            model=device.get("model") or "Unknown",
            name=f"Huawei {device.get('model') or device.get('role') or 'Device'}",
            serial_number=str(serial),
            sw_version=device.get("sw_version"),
            via_device=(DOMAIN, parent_identifier),
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _async_register_services(hass)
    return True


def _coordinator_for_device(
    hass: HomeAssistant, device_id: str
) -> EmmaCoordinator:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        _LOGGER.debug(
            "INPUT device validation=rejected device_id=%s reason=unknown_device",
            device_id,
        )
        raise ServiceValidationError(f"Unknown Home Assistant device: {device_id}")
    coordinators = hass.data.get(DOMAIN, {})
    for entry_id in device.config_entries:
        coordinator = coordinators.get(entry_id)
        if isinstance(coordinator, EmmaCoordinator):
            _LOGGER.debug(
                "INPUT device validation=accepted device_id=%s config_entry_id=%s",
                device_id,
                entry_id,
            )
            return coordinator
    _LOGGER.debug(
        "INPUT device validation=rejected device_id=%s "
        "reason=not_huawei_emma_device",
        device_id,
    )
    raise ServiceValidationError(
        f"Device {device_id} does not belong to a Huawei EMMA Management entry"
    )


def _coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> EmmaCoordinator:
    return _coordinator_for_device(hass, call.data["device_id"])


def _writable_control(
    coordinator: EmmaCoordinator, register_name: str
) -> dict[str, Any]:
    for description in coordinator.entity_descriptions:
        if description.get("register_name") != register_name:
            continue
        if not description.get("writeable"):
            break
        if (
            description.get("platform") in _SAFE_CONTROL_PLATFORMS
            or description.get("format") == "tou_periods"
        ):
            return description
        break
    raise ServiceValidationError(
        "Register is unknown, read-only, or not exposed as a safe control: "
        f"{register_name}"
    )


def _external_controls(coordinator: EmmaCoordinator) -> list[dict[str, Any]]:
    values = coordinator.data.get("values", {}) if coordinator.data else {}
    updated_at = coordinator.data.get("updated_at", {}) if coordinator.data else {}
    unsupported = (
        set(coordinator.data.get("unsupported", [])) if coordinator.data else set()
    )
    connected = bool(coordinator.data.get("health", {}).get("connected"))
    controls: list[dict[str, Any]] = []
    for description in coordinator.entity_descriptions:
        register_name = description.get("register_name")
        if not register_name or not description.get("writeable"):
            continue
        if (
            description.get("platform") not in _SAFE_CONTROL_PLATFORMS
            and description.get("format") != "tou_periods"
        ):
            continue
        controls.append(
            {
                key: value
                for key, value in {
                    "register_name": register_name,
                    "name": description.get("name"),
                    "platform": description.get("platform"),
                    "device_role": description.get("device_role"),
                    "value": values.get(register_name),
                    "updated_at": updated_at.get(register_name),
                    "available": connected
                    and register_name not in unsupported
                    and register_name in values,
                    "unit": description.get("unit"),
                    "minimum": description.get("minimum"),
                    "maximum": description.get("maximum"),
                    "step": description.get("step"),
                    "options": description.get("options"),
                    "format": description.get("format"),
                }.items()
                if value is not None
            }
        )
    return controls


async def _require_external_api_admin(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    user_id = call.context.user_id
    if user_id is None:
        _LOGGER.debug(
            "API authorization accepted domain=%s service=%s source=internal_context",
            call.domain,
            call.service,
        )
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.is_admin:
        _LOGGER.debug(
            "API authorization rejected domain=%s service=%s user_id=%s "
            "reason=administrator_required",
            call.domain,
            call.service,
            user_id,
        )
        raise ServiceValidationError(
            "The huawei_emma external API requires an administrator token"
        )
    _LOGGER.debug(
        "API authorization accepted domain=%s service=%s user_id=%s admin=true",
        call.domain,
        call.service,
        user_id,
    )


def _async_register_services(hass: HomeAssistant) -> None:
    async def set_tou_periods(call: ServiceCall) -> None:
        _LOGGER.debug(
            "SERVICE received domain=%s service=%s user_id=%s data=%r",
            call.domain,
            call.service,
            call.context.user_id,
            dict(call.data),
        )
        target_id = call.data["config_entry_id"]
        target = hass.data.get(DOMAIN, {}).get(target_id)
        if not isinstance(target, EmmaCoordinator):
            raise ServiceValidationError(
                f"Unknown Huawei EMMA config entry: {target_id}"
            )
        _LOGGER.debug(
            "SERVICE validation=accepted service=%s config_entry_id=%s action=write_tou",
            call.service,
            target_id,
        )
        await target.async_set_tou_schedule(
            call.data["periods"], source=f"{call.domain}.{call.service}"
        )
        _LOGGER.debug("SERVICE completed domain=%s service=%s", call.domain, call.service)

    async def read_time_segments(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug(
            "SERVICE received domain=%s service=%s user_id=%s device_id=%s "
            "action=read_time_segments",
            call.domain,
            call.service,
            call.context.user_id,
            call.data["device_id"],
        )
        target = _coordinator_for_call(hass, call)
        segments = target.growatt_time_segments()
        _LOGGER.debug(
            "SERVICE completed domain=%s service=%s result=%r",
            call.domain,
            call.service,
            segments,
        )
        return {"time_segments": segments}

    async def update_time_segment(call: ServiceCall) -> None:
        _LOGGER.debug(
            "SERVICE received domain=%s service=%s user_id=%s data=%r",
            call.domain,
            call.service,
            call.context.user_id,
            dict(call.data),
        )
        target = _coordinator_for_call(hass, call)
        if (
            call.domain == GROWATT_COMPAT_DOMAIN
            and not target.accept_external_growatt_controls
        ):
            _LOGGER.debug(
                "SERVICE validation=rejected domain=%s service=%s device_id=%s "
                "reason=external_growatt_controls_disabled",
                call.domain,
                call.service,
                call.data["device_id"],
            )
            raise ServiceValidationError(
                "External Growatt controls are disabled for this Huawei EMMA entry"
            )
        _LOGGER.debug(
            "SERVICE validation=accepted domain=%s service=%s action=translate_and_write",
            call.domain,
            call.service,
        )
        try:
            await target.async_update_growatt_time_segment(
                call.data["segment_id"],
                call.data["batt_mode"],
                call.data["start_time"],
                call.data["end_time"],
                call.data["enabled"],
            )
        except ValueError as error:
            _LOGGER.debug(
                "SERVICE validation=rejected domain=%s service=%s reason=%s",
                call.domain,
                call.service,
                error,
            )
            raise ServiceValidationError(str(error)) from error
        _LOGGER.debug("SERVICE completed domain=%s service=%s", call.domain, call.service)

    async def read_controls(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug(
            "API received domain=%s service=%s user_id=%s device_id=%s action=discover",
            call.domain,
            call.service,
            call.context.user_id,
            call.data["device_id"],
        )
        await _require_external_api_admin(hass, call)
        target = _coordinator_for_call(hass, call)
        controls = _external_controls(target)
        result = {
            "device": {
                "model": target.device_data.get("model"),
                "serial_number": target.device_data.get("serial_number"),
            },
            "accept_external_growatt_controls": (
                target.accept_external_growatt_controls
            ),
            "controls": controls,
        }
        _LOGGER.debug(
            "API completed domain=%s service=%s controls=%s",
            call.domain,
            call.service,
            len(controls),
        )
        return result

    async def set_external_value(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug(
            "API received domain=%s service=%s user_id=%s device_id=%s "
            "register=%s requested_value=%r",
            call.domain,
            call.service,
            call.context.user_id,
            call.data["device_id"],
            call.data["register_name"],
            call.data["value"],
        )
        await _require_external_api_admin(hass, call)
        target = _coordinator_for_call(hass, call)
        register_name = call.data["register_name"]
        try:
            description = _writable_control(target, register_name)
        except ServiceValidationError as error:
            _LOGGER.debug(
                "API validation=rejected service=%s register=%s reason=%s",
                call.service,
                register_name,
                error,
            )
            raise
        if register_name in set(target.data.get("unsupported", [])):
            _LOGGER.debug(
                "API validation=rejected service=%s register=%s reason=unsupported",
                call.service,
                register_name,
            )
            raise ServiceValidationError(
                "Register is unsupported by the connected Huawei system: "
                f"{register_name}"
            )
        _LOGGER.debug(
            "API validation=accepted service=%s register=%s platform=%s "
            "format=%s action=connector_write",
            call.service,
            register_name,
            description.get("platform"),
            description.get("format"),
        )
        try:
            if description.get("format") == "tou_periods":
                periods = call.data["value"]
                if not isinstance(periods, list):
                    raise ValueError("emma_tou_periods value must be a list")
                await target.async_set_tou_schedule(
                    periods, source=f"{call.domain}.{call.service}"
                )
                value: Any = target.data.get("values", {}).get(
                    TOU_REGISTER_NAME, periods
                )
            else:
                value = await target.async_set_value(
                    register_name,
                    call.data["value"],
                    source=f"{call.domain}.{call.service}",
                )
        except ValueError as error:
            _LOGGER.debug(
                "API validation=rejected service=%s register=%s reason=%s",
                call.service,
                register_name,
                error,
            )
            raise ServiceValidationError(str(error)) from error
        _LOGGER.info(
            "External huawei_emma API wrote register=%s device=%s",
            register_name,
            target.device_data.get("serial_number"),
        )
        _LOGGER.debug(
            "API completed domain=%s service=%s register=%s result=%r",
            call.domain,
            call.service,
            register_name,
            value,
        )
        return {"register_name": register_name, "value": value}

    async def set_luna_tou_periods(call: ServiceCall) -> None:
        _LOGGER.debug(
            "SERVICE received domain=%s service=%s user_id=%s device_id=%s "
            "format=luna_text periods=%r",
            call.domain,
            call.service,
            call.context.user_id,
            call.data["device_id"],
            call.data["periods"],
        )
        if call.domain == HUAWEI_EXTERNAL_API_DOMAIN:
            await _require_external_api_admin(hass, call)
        target = _coordinator_for_call(hass, call)
        try:
            periods = decode_luna_tou_periods(call.data["periods"])
            _LOGGER.debug(
                "SERVICE validation=accepted domain=%s service=%s "
                "format=luna_text decoded_periods=%r action=write_tou",
                call.domain,
                call.service,
                periods,
            )
            await target.async_set_tou_schedule(
                periods, source=f"{call.domain}.{call.service}"
            )
        except ValueError as error:
            _LOGGER.debug(
                "SERVICE validation=rejected domain=%s service=%s "
                "format=luna_text reason=%s",
                call.domain,
                call.service,
                error,
            )
            raise ServiceValidationError(str(error)) from error
        _LOGGER.info(
            "LUNA-compatible TOU schedule wrote periods=%s device=%s source=%s",
            len(periods),
            target.device_data.get("serial_number"),
            call.domain,
        )

    read_schema = _debug_schema(
        "read_time_segments_or_controls",
        vol.Schema({vol.Required("device_id"): cv.string}),
    )
    update_schema = _debug_schema(
        "update_time_segment",
        vol.Schema(
            {
                vol.Required("device_id"): cv.string,
                vol.Required("segment_id"): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=GROWATT_MAX_SEGMENTS)
                ),
                vol.Required("batt_mode"): vol.In(GROWATT_BATTERY_MODES),
                vol.Required("start_time"): cv.string,
                vol.Required("end_time"): cv.string,
                vol.Required("enabled"): cv.boolean,
            }
        ),
    )
    external_set_schema = _debug_schema(
        "huawei_emma.set_value",
        vol.Schema(
            {
                vol.Required("device_id"): cv.string,
                vol.Required("register_name"): cv.string,
                vol.Required("value"): object,
            }
        ),
    )
    luna_tou_schema = _debug_schema(
        "set_tou_periods_luna_text",
        vol.Schema(
            {
                vol.Required("device_id"): cv.string,
                vol.Required("periods"): cv.string,
            }
        ),
    )
    if not hass.services.has_service(DOMAIN, "set_tou_periods"):
        hass.services.async_register(
            DOMAIN,
            "set_tou_periods",
            set_tou_periods,
            schema=_debug_schema(
                "set_tou_periods",
                vol.Schema(
                    {
                        vol.Required("config_entry_id"): str,
                        vol.Required("periods"): [dict],
                    }
                ),
            ),
        )
    if not hass.services.has_service(DOMAIN, "read_time_segments"):
        hass.services.async_register(
            DOMAIN,
            "read_time_segments",
            read_time_segments,
            schema=read_schema,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, "update_time_segment"):
        hass.services.async_register(
            DOMAIN,
            "update_time_segment",
            update_time_segment,
            schema=update_schema,
        )

    if not hass.data.get(_GROWATT_ALIAS_OWNER):
        growatt_configured = bool(
            hass.config_entries.async_entries(GROWATT_COMPAT_DOMAIN)
        )
        growatt_services_exist = any(
            hass.services.has_service(GROWATT_COMPAT_DOMAIN, name)
            for name in ("read_time_segments", "update_time_segment")
        )
        if growatt_configured or growatt_services_exist:
            _LOGGER.warning(
                "Not registering growatt_server TOU aliases because the Growatt "
                "integration or its actions are already present; use %s actions instead",
                DOMAIN,
            )
        else:
            hass.services.async_register(
                GROWATT_COMPAT_DOMAIN,
                "read_time_segments",
                read_time_segments,
                schema=read_schema,
                supports_response=SupportsResponse.ONLY,
            )
            hass.services.async_register(
                GROWATT_COMPAT_DOMAIN,
                "update_time_segment",
                update_time_segment,
                schema=update_schema,
            )
            hass.data[_GROWATT_ALIAS_OWNER] = True
            _LOGGER.info(
                "Registered BESS-compatible growatt_server read/update "
                "time-segment aliases"
            )

    if not hass.data.get(_HUAWEI_API_OWNER):
        huawei_api_services_exist = any(
            hass.services.has_service(HUAWEI_EXTERNAL_API_DOMAIN, name)
            for name in _HUAWEI_API_SERVICE_NAMES
        )
        if huawei_api_services_exist:
            _LOGGER.warning(
                "Not registering huawei_emma external API because that service "
                "domain is already in use"
            )
        else:
            hass.services.async_register(
                HUAWEI_EXTERNAL_API_DOMAIN,
                "read_controls",
                read_controls,
                schema=read_schema,
                supports_response=SupportsResponse.ONLY,
            )
            hass.services.async_register(
                HUAWEI_EXTERNAL_API_DOMAIN,
                "set_value",
                set_external_value,
                schema=external_set_schema,
                supports_response=SupportsResponse.OPTIONAL,
            )
            hass.services.async_register(
                HUAWEI_EXTERNAL_API_DOMAIN,
                "set_tou_periods",
                set_luna_tou_periods,
                schema=luna_tou_schema,
            )
            hass.data[_HUAWEI_API_OWNER] = True
            _LOGGER.info(
                "Registered authenticated huawei_emma read_controls/set_value/"
                "set_tou_periods API"
            )

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].get(entry.entry_id)
        if (
            isinstance(coordinator, EmmaCoordinator)
            and coordinator.embedded_server is not None
        ):
            await coordinator.embedded_server.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            for service_name in _SERVICE_NAMES:
                if hass.services.has_service(DOMAIN, service_name):
                    hass.services.async_remove(DOMAIN, service_name)
            owns_aliases = hass.data.pop(_GROWATT_ALIAS_OWNER, False)
            if owns_aliases and not hass.config_entries.async_entries(
                GROWATT_COMPAT_DOMAIN
            ):
                for service_name in ("read_time_segments", "update_time_segment"):
                    if hass.services.has_service(GROWATT_COMPAT_DOMAIN, service_name):
                        hass.services.async_remove(GROWATT_COMPAT_DOMAIN, service_name)
            owns_huawei_api = hass.data.pop(_HUAWEI_API_OWNER, False)
            if owns_huawei_api:
                for service_name in _HUAWEI_API_SERVICE_NAMES:
                    if hass.services.has_service(
                        HUAWEI_EXTERNAL_API_DOMAIN, service_name
                    ):
                        hass.services.async_remove(
                            HUAWEI_EXTERNAL_API_DOMAIN, service_name
                        )
    return unloaded


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Mark pre-dual-mode entries as external connector configurations."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_MODE: MODE_EXTERNAL, **entry.data},
            version=2,
        )
        _LOGGER.info("Migrated Huawei EMMA config entry to external mode")
    return True


def _config_path(hass: HomeAssistant, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(hass.config.path(value))
