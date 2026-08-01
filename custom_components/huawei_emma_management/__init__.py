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
from homeassistant.helpers import config_validation as cv, service as service_helper
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
from .tou import decode_luna_tou_periods, encode_luna_tou_periods


_LOGGER = logging.getLogger(__name__)
_GROWATT_ALIAS_OWNER = f"{DOMAIN}_owns_growatt_aliases"
_HUAWEI_API_OWNER = f"{DOMAIN}_owns_huawei_api"
_HUAWEI_API_SERVICE_NAMES = (
    "read_controls",
    "set_value",
    "set_tou_periods",
    "read_time_segments",
    "update_time_segment",
)
_LEGACY_MANAGEMENT_SERVICE_NAMES = (
    "set_tou_periods",
    "read_time_segments",
    "update_time_segment",
)
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


def _require_tou_input(data: dict[str, Any]) -> dict[str, Any]:
    if "periods" not in data and "structured_periods" not in data:
        raise vol.Invalid("either periods or structured_periods is required")
    return data


def _set_huawei_service_schemas(hass: HomeAssistant) -> None:
    """Describe the dynamically registered public huawei_emma actions."""
    parent_device_ids = _configured_parent_device_ids(hass)
    device_field = {
        "name": "Device",
        "description": (
            "The Huawei EMMA device to use. This may be omitted when exactly "
            "one Huawei EMMA integration entry is configured."
        ),
        "required": False,
        "selector": {"device": {"integration": DOMAIN}},
    }
    if len(parent_device_ids) == 1:
        # Home Assistant uses field examples for "Fill example data". Device
        # registry IDs are installation-specific, so discover the real one.
        device_field["example"] = parent_device_ids[0]
    schemas = {
        "read_controls": {
            "name": "Read EMMA controls",
            "description": (
                "Return all currently exposed writable controls, values, and limits."
            ),
            "fields": {"device_id": device_field},
        },
        "set_value": {
            "name": "Set EMMA value",
            "description": (
                "Validate and write one exposed control and optionally return its readback."
            ),
            "fields": {
                "device_id": device_field,
                "register_name": {
                    "name": "Register name",
                    "required": True,
                    "example": "storage_maximum_charging_power",
                    "selector": {"text": {}},
                },
                "value": {
                    "name": "Value",
                    "description": (
                        "Number, boolean, enum key, timestamp, or structured value "
                        "accepted by the selected control."
                    ),
                    "required": True,
                    "example": 5000,
                    "selector": {"object": {}},
                },
            },
        },
        "set_tou_periods": {
            "name": "Set EMMA TOU periods",
            "description": (
                "Replace the complete TOU schedule using either LUNA text or a "
                "structured period list. Specify exactly one format. The optional "
                "response contains the schedule read back from EMMA."
            ),
            "fields": {
                "device_id": device_field,
                "periods": {
                    "name": "LUNA text periods",
                    "description": (
                        "One HH:MM-HH:MM/DAYS/+|- period per line; + charges and "
                        "- discharges. Leave this out when using structured periods."
                    ),
                    "example": (
                        "00:00-03:59/1234567/+\n"
                        "07:00-09:59/1234567/-"
                    ),
                    "selector": {"text": {"multiline": True}},
                },
                "structured_periods": {
                    "name": "Structured periods",
                    "description": (
                        "List of periods with start_time, end_time, charge_flag, "
                        "and seven days_effective booleans. Leave this out when "
                        "using LUNA text. It is intentionally not included in "
                        "Fill example data because Home Assistant cannot express "
                        "the either/or field relationship."
                    ),
                    "selector": {"object": {}},
                },
            },
        },
        "read_time_segments": {
            "name": "Read EMMA time segments",
            "description": "Return the nine-slot BESS-compatible TOU schedule.",
            "fields": {"device_id": device_field},
        },
        "update_time_segment": {
            "name": "Update EMMA time segment",
            "description": (
                "Update one BESS-compatible slot and immediately write the full schedule."
            ),
            "fields": {
                "device_id": device_field,
                "segment_id": {
                    "name": "Segment",
                    "required": True,
                    "example": 1,
                    "selector": {
                        "number": {"min": 1, "max": 9, "step": 1, "mode": "box"}
                    },
                },
                "batt_mode": {
                    "name": "Battery mode",
                    "required": True,
                    "example": "battery_first",
                    "selector": {
                        "select": {"options": list(GROWATT_BATTERY_MODES)}
                    },
                },
                "start_time": {
                    "name": "Start time",
                    "required": True,
                    "example": "00:00",
                    "selector": {"time": {}},
                },
                "end_time": {
                    "name": "End time",
                    "required": True,
                    "example": "06:00",
                    "selector": {"time": {}},
                },
                "enabled": {
                    "name": "Enabled",
                    "required": True,
                    "example": True,
                    "selector": {"boolean": {}},
                },
            },
        },
    }
    for service_name, schema in schemas.items():
        service_helper.async_set_service_schema(
            hass, HUAWEI_EXTERNAL_API_DOMAIN, service_name, schema
        )


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


def _configured_coordinators(hass: HomeAssistant) -> list[EmmaCoordinator]:
    return [
        coordinator
        for coordinator in hass.data.get(DOMAIN, {}).values()
        if isinstance(coordinator, EmmaCoordinator)
    ]


def _parent_device_id_for_coordinator(
    hass: HomeAssistant, coordinator: EmmaCoordinator
) -> str | None:
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, coordinator.parent_identifier)}
    )
    return device.id if device is not None else None


def _configured_parent_device_ids(hass: HomeAssistant) -> list[str]:
    return [
        device_id
        for coordinator in _configured_coordinators(hass)
        if (device_id := _parent_device_id_for_coordinator(hass, coordinator))
        is not None
    ]


def _coordinator_for_call(hass: HomeAssistant, call: ServiceCall) -> EmmaCoordinator:
    device_id = call.data.get("device_id")
    if device_id:
        return _coordinator_for_device(hass, device_id)

    coordinators = _configured_coordinators(hass)
    if len(coordinators) == 1:
        _LOGGER.debug(
            "INPUT device validation=accepted device_id=auto "
            "config_entry_id=%s reason=single_huawei_emma_entry",
            coordinators[0].entry_id,
        )
        return coordinators[0]
    if not coordinators:
        reason = "no Huawei EMMA entries are loaded"
    else:
        reason = "multiple Huawei EMMA entries are loaded"
    _LOGGER.debug(
        "INPUT device validation=rejected device_id=missing reason=%s", reason
    )
    raise ServiceValidationError(f"device_id is required because {reason}")


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
    for legacy_service in _LEGACY_MANAGEMENT_SERVICE_NAMES:
        if hass.services.has_service(DOMAIN, legacy_service):
            hass.services.async_remove(DOMAIN, legacy_service)
            _LOGGER.info(
                "Removed legacy %s.%s action; use %s.%s",
                DOMAIN,
                legacy_service,
                HUAWEI_EXTERNAL_API_DOMAIN,
                legacy_service,
            )

    async def read_time_segments(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug(
            "SERVICE received domain=%s service=%s user_id=%s device_id=%s "
            "action=read_time_segments",
            call.domain,
            call.service,
            call.context.user_id,
            call.data.get("device_id", "auto"),
        )
        if call.domain == HUAWEI_EXTERNAL_API_DOMAIN:
            await _require_external_api_admin(hass, call)
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
        if call.domain == HUAWEI_EXTERNAL_API_DOMAIN:
            await _require_external_api_admin(hass, call)
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
                call.data.get("device_id", "auto"),
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
            call.data.get("device_id", "auto"),
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
            call.data.get("device_id", "auto"),
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

    async def set_tou_periods(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug(
            "SERVICE received domain=%s service=%s user_id=%s device_id=%s "
            "periods=%r structured_periods=%r",
            call.domain,
            call.service,
            call.context.user_id,
            call.data.get("device_id", "auto"),
            call.data.get("periods"),
            call.data.get("structured_periods"),
        )
        if call.domain == HUAWEI_EXTERNAL_API_DOMAIN:
            await _require_external_api_admin(hass, call)
        target = _coordinator_for_call(hass, call)
        try:
            if "periods" in call.data:
                periods = decode_luna_tou_periods(call.data["periods"])
                input_format = "luna_text"
            else:
                periods = call.data["structured_periods"]
                input_format = "structured"
            _LOGGER.debug(
                "SERVICE validation=accepted domain=%s service=%s "
                "format=%s decoded_periods=%r action=write_tou",
                call.domain,
                call.service,
                input_format,
                periods,
            )
            readback = await target.async_set_tou_schedule(
                periods, source=f"{call.domain}.{call.service}"
            )
        except ValueError as error:
            _LOGGER.debug(
                "SERVICE validation=rejected domain=%s service=%s "
                "reason=%s",
                call.domain,
                call.service,
                error,
            )
            raise ServiceValidationError(str(error)) from error
        _LOGGER.info(
            "TOU schedule wrote format=%s periods=%s device=%s source=%s",
            input_format,
            len(periods),
            target.device_data.get("serial_number"),
            call.domain,
        )
        return {
            "device_id": call.data.get("device_id")
            or _parent_device_id_for_coordinator(hass, target),
            "periods": encode_luna_tou_periods(readback),
            "structured_periods": readback,
        }

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
                vol.Optional("device_id"): cv.string,
                vol.Required("register_name"): cv.string,
                vol.Required("value"): object,
            }
        ),
    )
    luna_tou_schema = _debug_schema(
        "huawei_emma.set_tou_periods",
        vol.All(
            vol.Schema(
                {
                    vol.Optional("device_id"): cv.string,
                    vol.Exclusive("periods", "tou_format"): cv.string,
                    vol.Exclusive("structured_periods", "tou_format"): [dict],
                }
            ),
            _require_tou_input,
        ),
    )
    huawei_read_schema = _debug_schema(
        "huawei_emma.read",
        vol.Schema({vol.Optional("device_id"): cv.string}),
    )
    huawei_update_schema = _debug_schema(
        "huawei_emma.update_time_segment",
        vol.Schema(
            {
                vol.Optional("device_id"): cv.string,
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
                HUAWEI_EXTERNAL_API_DOMAIN,
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
                schema=huawei_read_schema,
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
                set_tou_periods,
                schema=luna_tou_schema,
                supports_response=SupportsResponse.OPTIONAL,
            )
            hass.services.async_register(
                HUAWEI_EXTERNAL_API_DOMAIN,
                "read_time_segments",
                read_time_segments,
                schema=huawei_read_schema,
                supports_response=SupportsResponse.ONLY,
            )
            hass.services.async_register(
                HUAWEI_EXTERNAL_API_DOMAIN,
                "update_time_segment",
                update_time_segment,
                schema=huawei_update_schema,
            )
            hass.data[_HUAWEI_API_OWNER] = True
            _LOGGER.info(
                "Registered authenticated huawei_emma read_controls/set_value/"
                "set_tou_periods/read_time_segments/update_time_segment API"
            )
    if hass.data.get(_HUAWEI_API_OWNER):
        # Refresh installation-specific device examples when entries are added.
        _set_huawei_service_schemas(hass)


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
        elif hass.data.get(_HUAWEI_API_OWNER):
            # Refresh the auto-filled device ID after one of several entries is removed.
            _set_huawei_service_schemas(hass)
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
