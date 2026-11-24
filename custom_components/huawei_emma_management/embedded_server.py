#!/usr/bin/env python3
"""Huawei EMMA reverse Modbus/TLS server shared by HA and standalone mode."""

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import importlib.metadata
import json
import logging
import os
import secrets
import re
import ssl
import struct
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from huawei_solar import register_names as rn
from huawei_solar import register_values as rv
from huawei_solar.device import create_device_instance
from huawei_solar.exceptions import DecodeError, HuaweiSolarException, ReadException
from huawei_solar.modbus_client import AsyncHuaweiSolarClient
from huawei_solar.register_definitions import Result
from huawei_solar.register_definitions.periods import (
    ChargeFlag,
    HUAWEI_LUNA2000_TimeOfUsePeriod,
)
from huawei_solar.registers import REGISTERS
from tmodbus.exceptions import (
    InvalidResponseError,
    ModbusConnectionError,
    UnknownModbusResponseError,
    error_code_to_exception_map,
)
from tmodbus.pdu import BaseClientPDU
from tmodbus.transport.async_base import AsyncBaseTransport

try:
    from .connector_contract import (
        API_PREFIX,
        CONNECTOR_VERSION,
        DEFAULT_API_PORT,
        DEFAULT_TLS_PORT,
        MAX_HTTP_REQUEST_BODY,
        MAX_MBAP_LENGTH,
        MAX_READ_REGISTERS,
        MAX_WRITE_REGISTERS,
    )
    from .embedded_catalog import (
        CAPABILITY_MASK_REGISTERS,
        POLL_INTERVALS,
        EntityDescription,
        build_entity_catalog,
        capability_state,
        capability_states,
        catalog_sha256,
        grouped_register_names,
        json_value,
        timestamp_epoch,
    )
    from .embedded_runtime_setup import (
        CertificateStatus,
        certificate_days_remaining,
        ensure_certificates,
        load_or_create_environment,
    )
    from . import tou_codec
except ImportError:  # Standalone external-server launcher.
    from connector_contract import (
        API_PREFIX,
        CONNECTOR_VERSION,
        DEFAULT_API_PORT,
        DEFAULT_TLS_PORT,
        MAX_HTTP_REQUEST_BODY,
        MAX_MBAP_LENGTH,
        MAX_READ_REGISTERS,
        MAX_WRITE_REGISTERS,
    )
    from embedded_catalog import (
        CAPABILITY_MASK_REGISTERS,
        POLL_INTERVALS,
        EntityDescription,
        build_entity_catalog,
        capability_state,
        capability_states,
        catalog_sha256,
        grouped_register_names,
        json_value,
        timestamp_epoch,
    )
    from embedded_runtime_setup import (
        CertificateStatus,
        certificate_days_remaining,
        ensure_certificates,
        load_or_create_environment,
    )
    import tou_codec

log = logging.getLogger(__name__)

# Connector runtime version; kept equal to the Home Assistant manifest version.


def _package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None

API_HEALTH = f"{API_PREFIX}/health"
API_DEVICE = f"{API_PREFIX}/device"
API_ENTITIES = f"{API_PREFIX}/entities"
API_STATES = f"{API_PREFIX}/states"
API_SUBSCRIPTIONS = f"{API_PREFIX}/subscriptions"
API_TOU_PERIODS = f"{API_PREFIX}/tou-periods"
API_DIAGNOSTICS = f"{API_PREFIX}/diagnostics"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = DEFAULT_TLS_PORT
DEFAULT_CERTFILE = Path("certs/server-cert.pem")
DEFAULT_KEYFILE = Path("certs/server-key.pem")
DEFAULT_CA_CERTFILE = Path("certs/ca-cert.pem")
DEFAULT_CA_KEYFILE = Path("certs/ca-key.pem")


FUNCTION_NAMES = {
    0x03: "Read Holding Registers",
    0x06: "Write Single Register",
    0x10: "Write Multiple Registers",
    0x2B: "Read Device Identification",
    0x41: "Huawei-defined",
}


@dataclass(slots=True)
class ModbusFrame:
    transaction_id: int
    protocol_id: int
    unit_id: int
    pdu: bytes

    @property
    def function_code(self) -> int | None:
        return self.pdu[0] if self.pdu else None

    def encode(self) -> bytes:
        if not self.pdu:
            raise ValueError("A Modbus frame must contain a PDU")

        length = 1 + len(self.pdu)
        if length > 0xFFFF:
            raise ValueError("Modbus frame is too large")

        return struct.pack(">HHHB", self.transaction_id, self.protocol_id, length, self.unit_id) + self.pdu


class ModbusProtocolError(RuntimeError):
    pass


class ModbusExceptionResponse(ModbusProtocolError):
    def __init__(self, function_code: int, exception_code: int):
        self.function_code = function_code
        self.exception_code = exception_code
        super().__init__(
            f"Modbus exception response function=0x{function_code:02X} "
            f"exception=0x{exception_code:02X}"
        )


async def read_modbus_frame(reader: asyncio.StreamReader) -> ModbusFrame:
    header = await reader.readexactly(6)
    transaction_id, protocol_id, length = struct.unpack(">HHH", header)

    if length < 2:
        raise ModbusProtocolError(f"Invalid MBAP length {length}; expected at least 2")
    if length > MAX_MBAP_LENGTH:
        raise ModbusProtocolError(
            f"Invalid MBAP length {length}; maximum accepted length is {MAX_MBAP_LENGTH}"
        )

    body = await reader.readexactly(length)
    return ModbusFrame(transaction_id, protocol_id, body[0], body[1:])


def describe_frame(frame: ModbusFrame, request_pdu: bytes | None = None) -> str:
    function_code = frame.function_code
    if function_code is None:
        function = "empty PDU"
    else:
        base_code = function_code & 0x7F if function_code & 0x80 else function_code
        function = FUNCTION_NAMES.get(base_code, "Unknown function")
        function = f"fc=0x{function_code:02X} ({function})"

    details = _describe_pdu_details(frame.pdu, request_pdu)
    return (
        f"transaction={frame.transaction_id} protocol={frame.protocol_id} "
        f"unit={frame.unit_id} {function}{details}"
    )


def _describe_pdu_details(pdu: bytes, request_pdu: bytes | None = None) -> str:
    if not pdu:
        return ""
    function_code = pdu[0]
    base_code = function_code & 0x7F if function_code & 0x80 else function_code
    context = request_pdu if request_pdu else pdu

    if base_code == 0x03 and len(context) >= 5 and context[0] == 0x03:
        address, count = struct.unpack(">HH", context[1:5])
        suffix = f" address={address} (0x{address:04X}) count={count}"
        if function_code == 0x03 and request_pdu is not None and len(pdu) >= 2:
            suffix += f" byte_count={pdu[1]}"
        elif function_code & 0x80 and len(pdu) >= 2:
            suffix += f" exception={pdu[1]}"
        return suffix

    if base_code == 0x06 and len(context) >= 5 and context[0] == 0x06:
        address, value = struct.unpack(">HH", context[1:5])
        return f" address={address} (0x{address:04X}) value={value}"

    if base_code == 0x10 and len(context) >= 5 and context[0] == 0x10:
        address, count = struct.unpack(">HH", context[1:5])
        return f" address={address} (0x{address:04X}) count={count}"

    if base_code == 0x2B and len(context) >= 4 and context[:3] == b"\x2b\x0e\x03":
        return f" object_id=0x{context[3]:02X}"
    return ""


DEVICE_INFO_PATTERN = re.compile(rb"1=[\x20-\x7e]{4,}")


def parse_huawei_device_info(pdu: bytes) -> dict[str, str]:
    """Extract Huawei's semicolon-delimited device description from a private PDU."""
    match = DEVICE_INFO_PATTERN.search(pdu)
    if not match:
        return {}

    text = match.group(0).split(b"\x00", 1)[0].decode("ascii", errors="replace")
    result: dict[str, str] = {}
    for item in text.split(";"):
        key, separator, value = item.partition("=")
        if separator and key.isdigit():
            result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class DeviceIdentificationPage:
    more_follows: bool
    next_object_id: int
    reported_object_count: int
    objects: dict[int, bytes]


def parse_device_identification_page(pdu: bytes) -> DeviceIdentificationPage:
    if len(pdu) < 7 or pdu[:3] != b"\x2b\x0e\x03":
        raise ModbusProtocolError(
            f"Unexpected device-list response PDU: {pdu.hex(' ')}"
        )

    more_follows = pdu[4] != 0
    next_object_id = pdu[5]
    object_count = pdu[6]
    offset = 7
    objects: dict[int, bytes] = {}
    # EMMA reports the total object count here even when only a single object is
    # present in this paged response. Parse the objects actually on the wire.
    while offset < len(pdu):
        if offset + 2 > len(pdu):
            raise ModbusProtocolError("Truncated object header in device-list response")
        object_id = pdu[offset]
        object_length = pdu[offset + 1]
        offset += 2
        if offset + object_length > len(pdu):
            raise ModbusProtocolError("Truncated object value in device-list response")
        objects[object_id] = pdu[offset : offset + object_length]
        offset += object_length
    return DeviceIdentificationPage(
        more_follows=more_follows,
        next_object_id=next_object_id,
        reported_object_count=object_count,
        objects=objects,
    )


def parse_device_identification_objects(pdu: bytes) -> dict[int, bytes]:
    """Compatibility wrapper returning only the objects in one response page."""
    return parse_device_identification_page(pdu).objects


def parse_topology_devices(objects: dict[int, bytes]) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for object_id, payload in sorted(objects.items()):
        if object_id == 0x87:
            continue
        info = parse_huawei_device_info(payload)
        if not info:
            continue
        model = info.get("1", "Unknown")
        product_type = info.get("8")
        devices.append(
            {
                "object_id": f"0x{object_id:02X}",
                "role": _topology_device_role(model, product_type),
                "model": model,
                "sw_version": info.get("2"),
                "protocol_version": info.get("3"),
                "serial_number": info.get("4"),
                "device_type": info.get("5"),
                "unit_id": int(info["5"]) if info.get("5", "").isdigit() else None,
                "interface_version": info.get("6"),
                "product_type": product_type,
            }
        )
    return devices


def _topology_device_role(model: str, product_type: str | None) -> str:
    """Classify a device-list entry.

    Unrecognized devices become ``accessory``. No catalog register targets that
    role, so an unknown device is never polled and never falls back to EMMA.
    """
    text = f"{model} {product_type or ''}".lower()
    if "emma" in text or "hems" in text:
        return "emma"
    if "smartguard" in text or "backupbox" in text or "backup box" in text:
        return "smartguard"
    if "sun2000" in text or "inverter" in text:
        return "inverter"
    if "smartlogger" in text:
        return "smartlogger"
    if "sdongle" in text or "smart dongle" in text:
        return "sdongle"
    if any(word in text for word in ("meter", "dtsu", "ddsu", "yds", "energy sensor")):
        return "external_meter"
    if any(word in text for word in ("acgun", "ac gun", "ac-gun", "smart_ac_gun")):
        return "smart_ac_gun"
    if "charger" in text or "wallbox" in text:
        return "charger"
    return "accessory"


# Downstream device roles that are read through their own Modbus unit ID.
SUBDEVICE_ROLES = (
    "inverter",
    "smartguard",
    "charger",
    "smart_ac_gun",
    "sdongle",
    "smartlogger",
)


def expand_subscriptions(register_names: set[str]) -> set[str]:
    """Return the effective poll set for requested registers.

    Adds registers needed to interpret requested ones (for example the UTC
    clock used to normalize a local wall-clock timestamp) and the feature-mask
    registers used for capability-gated writes. Registers of absent devices
    are filtered later by topology, so this never adds Modbus traffic for
    hardware that is not installed.
    """
    effective = set(register_names)
    for register_name in register_names:
        description = ENTITY_CATALOG.get(register_name)
        if description is not None:
            effective.update(description.depends_on)
    effective.update(CAPABILITY_MASK_REGISTERS)
    return {name for name in effective if name in ENTITY_CATALOG}


# Modbus function codes that change device state. Read-only mode blocks them
# at the lowest request layer, so no code path can write by accident.
MODBUS_WRITE_FUNCTION_CODES = frozenset({0x05, 0x06, 0x0F, 0x10, 0x15, 0x16, 0x17})
READ_ONLY_MESSAGE = (
    "EMMA read-only mode is enabled: writes are disabled because EMMA and the "
    "battery are controlled by an external provider"
)


class ReadOnlyModeError(PermissionError):
    """A write was attempted while EMMA read-only mode is enabled."""

    def __init__(self, message: str = READ_ONLY_MESSAGE) -> None:
        super().__init__(message)


class WriteVerificationError(RuntimeError):
    """The device accepted a write but the readback does not match."""


def _identity_transform(_register_name: Any, result: Result[Any]) -> Result[Any]:
    return result


def _use_raw_register_values(device: Any) -> None:
    """Disable host-dependent upstream value transforms on a device instance.

    huawei-solar 3.2 converts local wall-clock timestamps using the host time
    zone when it happens to match the device. The connector contract instead
    publishes raw register seconds in every runtime (Python and ESPHome) and
    normalizes them deterministically from the device's own clock registers.
    """
    if device is not None:
        device._transform_register_values = _identity_transform


@dataclass(frozen=True, slots=True)
class SensorDefinition:
    register_name: rn.RegisterName
    entity_id: str
    name: str
    unit: str
    device_class: str | None = None
    state_class: str = "measurement"
    multiplier: float = 1.0


CORE_SENSORS = (
    SensorDefinition(
        rn.PV_OUTPUT_POWER,
        "sensor.huawei_emma_pv_output_power",
        "Huawei EMMA PV Output Power",
        "kW",
        "power",
        multiplier=0.001,
    ),
    SensorDefinition(
        rn.LOAD_POWER,
        "sensor.huawei_emma_load_power",
        "Huawei EMMA Load Power",
        "kW",
        "power",
        multiplier=0.001,
    ),
    SensorDefinition(
        rn.FEED_IN_POWER,
        "sensor.huawei_emma_grid_feed_in_power",
        "Huawei EMMA Grid Feed-in Power",
        "kW",
        "power",
        multiplier=0.001,
    ),
    SensorDefinition(
        rn.BATTERY_CHARGE_DISCHARGE_POWER,
        "sensor.huawei_emma_battery_power",
        "Huawei EMMA Battery Charge Discharge Power",
        "kW",
        "power",
        multiplier=0.001,
    ),
    SensorDefinition(
        rn.INVERTER_RATED_POWER,
        "sensor.huawei_emma_inverter_rated_power",
        "Huawei EMMA Inverter Rated Power",
        "kW",
        "power",
        multiplier=0.001,
    ),
    SensorDefinition(
        rn.INVERTER_ACTIVE_POWER,
        "sensor.huawei_emma_inverter_active_power",
        "Huawei EMMA Inverter Active Power",
        "kW",
        "power",
        multiplier=0.001,
    ),
    SensorDefinition(
        rn.STATE_OF_CAPACITY,
        "sensor.huawei_emma_battery_state_of_capacity",
        "Huawei EMMA Battery State of Capacity",
        "%",
        "battery",
    ),
    SensorDefinition(
        rn.ESS_CHARGEABLE_CAPACITY,
        "sensor.huawei_emma_ess_chargeable_energy",
        "Huawei EMMA ESS Chargeable Energy",
        "kWh",
        "energy_storage",
    ),
    SensorDefinition(
        rn.ESS_DISCHARGEABLE_CAPACITY,
        "sensor.huawei_emma_ess_dischargeable_energy",
        "Huawei EMMA ESS Dischargeable Energy",
        "kWh",
        "energy_storage",
    ),
    SensorDefinition(
        rn.BACKUP_POWER_STATE_OF_CHARGE,
        "sensor.huawei_emma_backup_power_state_of_capacity",
        "Huawei EMMA Backup Power State of Capacity",
        "%",
        "battery",
    ),
)

CORE_REGISTER_NAMES = tuple(sensor.register_name for sensor in CORE_SENSORS)
ENTITY_CATALOG = build_entity_catalog()
CATALOG_SHA256 = catalog_sha256(ENTITY_CATALOG)
POLL_GROUPS = grouped_register_names(ENTITY_CATALOG)
INVERTER_RATED_LIMITED_REGISTERS = {
    str(rn.STORAGE_MAXIMUM_CHARGING_POWER),
    str(rn.STORAGE_MAXIMUM_DISCHARGING_POWER),
    str(rn.STORAGE_FORCIBLE_CHARGE_POWER),
    str(rn.STORAGE_FORCIBLE_DISCHARGE_POWER),
}


def build_core_sensor_states(
    results: dict[rn.RegisterName, Result[Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for sensor in CORE_SENSORS:
        decoded = results[sensor.register_name]
        state = round(float(decoded.value) * sensor.multiplier, 3)
        attributes: dict[str, Any] = {
            "friendly_name": sensor.name,
            "unit_of_measurement": sensor.unit,
            "state_class": sensor.state_class,
            "source": "Huawei EMMA reverse Modbus/TLS",
            "huawei_register": str(sensor.register_name),
        }
        if sensor.device_class:
            attributes["device_class"] = sensor.device_class
        result[sensor.entity_id] = {"state": state, "attributes": attributes}
    return result


def normalize_register_value(register_name: str, value: Any) -> Any:
    """Return the connector JSON value for a decoded register value."""
    normalized = json_value(value)
    description = ENTITY_CATALOG.get(str(register_name))
    if description is not None and description.format == "tou_periods":
        return tou_codec.canonical_readback(normalized)
    return normalized


def build_catalog_states(
    results: dict[str, Result[Any]],
    context_values: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Create REST-compatible sensor states and API-native register values."""
    states: dict[str, dict[str, Any]] = {}
    values: dict[str, Any] = {}
    for register_name, decoded in results.items():
        description = ENTITY_CATALOG.get(str(register_name))
        if description is None:
            continue
        value = normalize_register_value(str(register_name), decoded.value)
        values[str(register_name)] = value
    context = {**(context_values or {}), **values}
    for register_name, value in values.items():
        description = ENTITY_CATALOG[register_name]
        if description.platform != "sensor":
            continue
        attributes: dict[str, Any] = {
            "friendly_name": f"Huawei EMMA {description.name}",
            "source": "Huawei EMMA reverse Modbus/TLS",
            "huawei_register": str(register_name),
            "register_address": description.address,
        }
        if description.unit:
            attributes["unit_of_measurement"] = description.unit
        if description.device_class:
            attributes["device_class"] = description.device_class
        if description.state_class:
            attributes["state_class"] = description.state_class
        if description.icon:
            attributes["icon"] = description.icon
        state_value = value
        if description.format == "tou_periods":
            attributes["periods"] = value
            attributes.update(tou_codec.numbered_period_attributes(value))
            state_value = len(value) if isinstance(value, list) else None
        elif description.device_class == "timestamp" and value is not None:
            epoch = timestamp_epoch(register_name, context, description.format)
            state_value = (
                datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
                if epoch is not None
                else None
            )
        states[f"sensor.huawei_emma_{register_name}"] = {
            "state": state_value,
            "attributes": attributes,
        }
    return states, values


class HomeAssistantPublisher:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 5.0,
        ca_file: Path | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)

    async def publish(self, states: dict[str, dict[str, Any]]) -> None:
        results = await asyncio.gather(
            *(asyncio.to_thread(self._publish_one, entity_id, body) for entity_id, body in states.items()),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            log.warning(
                "Home Assistant update failed for %d/%d entities: %s",
                len(failures),
                len(states),
                failures[0],
            )

    def _publish_one(self, entity_id: str, body: dict[str, Any]) -> None:
        entity_path = urllib.parse.quote(entity_id, safe="._")
        request = urllib.request.Request(
            f"{self.base_url}/api/states/{entity_path}",
            data=json.dumps(body, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.ssl_context
            ) as response:
                if response.status not in (200, 201):
                    raise RuntimeError(
                        f"Home Assistant returned HTTP {response.status} for {entity_id}"
                    )
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Home Assistant returned HTTP {error.code} for {entity_id}"
            ) from error


RT = TypeVar("RT")


def _modbus_response_error(exception_code: int, function_code: int) -> Exception:
    """Build the tmodbus exception for a Modbus exception response.

    tmodbus 1.x exception classes take only the function code (the error code
    is a class attribute); only UnknownModbusResponseError takes both.
    """
    error_class = error_code_to_exception_map.get(exception_code)
    if error_class is None:
        return UnknownModbusResponseError(exception_code, function_code)
    return error_class(function_code)


class ReverseConnectionTransport(AsyncBaseTransport):
    """tmodbus transport over the TLS socket that EMMA opened toward us."""

    def __init__(self, session: "ReverseModbusSession"):
        self.session = session

    async def open(self) -> None:
        if self.session.closed:
            raise ModbusConnectionError("EMMA reverse connection is closed")

    async def close(self) -> None:
        self.session.writer.close()
        with contextlib.suppress(Exception):
            await self.session.writer.wait_closed()

    def is_open(self) -> bool:
        return not self.session.closed

    async def send_and_receive(self, unit_id: int, pdu: BaseClientPDU[RT]) -> RT:
        try:
            response = await self.session.request(unit_id, pdu.encode_request())
        except ModbusExceptionResponse as error:
            raise _modbus_response_error(
                error.exception_code, error.function_code & 0x7F
            ) from error
        except ConnectionError as error:
            raise ModbusConnectionError(str(error)) from error
        except ModbusProtocolError as error:
            raise InvalidResponseError(str(error), response_bytes=b"") from error

        if response.unit_id != unit_id:
            message = (
                f"Unit ID mismatch: expected {unit_id:#04x}, "
                f"received {response.unit_id:#04x}"
            )
            raise InvalidResponseError(message, response_bytes=response.encode())
        return pdu.decode_response(response.pdu)


class ReverseModbusSession:
    """Modbus master operating over a socket initiated by the Huawei slave."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        request_timeout: float,
        poll_interval: float = 30.0,
        poll_intervals: dict[str, float] | None = None,
        publisher: HomeAssistantPublisher | None,
        state_callback: Callable[["ReverseModbusSession", dict[str, dict[str, Any]]], Awaitable[None]],
        subscribed_registers: set[str] | None = None,
        log_raw: bool = False,
        once: bool = False,
        read_only: bool = False,
    ):
        self.read_only = read_only
        self.reader = reader
        self.writer = writer
        self.request_timeout = request_timeout
        self.poll_interval = poll_interval
        self.poll_intervals = poll_intervals or {
            "fast": poll_interval,
            "medium": max(poll_interval, 300.0),
            "slow": max(poll_interval, 1800.0),
        }
        self.publisher = publisher
        self.state_callback = state_callback
        self.log_raw = log_raw
        self.once = once
        self.peer = writer.get_extra_info("peername")
        self.device_info: dict[str, str] = {}
        self.device_objects: dict[int, bytes] = {}
        self.topology_devices: list[dict[str, Any]] = []
        self.available_device_roles: set[str] = set()
        self.topology_complete = False
        self.latest_states: dict[str, dict[str, Any]] = {}
        self.latest_values: dict[str, Any] = {}
        self.latest_updated_at: dict[str, str] = {}
        self.unsupported_registers: set[str] = set()
        # Registers that exist in the catalog but do not apply to the detected
        # hardware (for example SmartGuard phase B/C on single-phase units).
        self.not_applicable_registers: set[str] = set()
        # Registers whose value could not be decoded (for example an enum
        # value unknown to huawei-solar); they are published as raw integers.
        self.raw_value_registers: set[str] = set()
        self.subscribed_registers = expand_subscriptions(
            set(
                subscribed_registers
                if subscribed_registers is not None
                else default_subscriptions()
            )
        )
        self.device_details: dict[str, dict[str, Any]] = {}
        self.poll_stats: dict[str, dict[str, Any]] = {
            group: {
                "runs": 0,
                "failures": 0,
                "last_duration_ms": None,
                "last_success": None,
                "last_error": None,
                "registers_requested": 0,
                "registers_updated": 0,
            }
            for group in POLL_GROUPS
        }
        self.last_verified_write: dict[str, Any] | None = None
        self.last_poll_success: str | None = None
        self.last_poll_error: str | None = None
        self.device: Any | None = None
        self._pending: dict[int, asyncio.Future[ModbusFrame]] = {}
        self._request_pdus: dict[int, bytes] = {}
        self._next_transaction_id = 1
        self._request_lock = asyncio.Lock()
        self._startup_frame = asyncio.Event()
        self._subscriptions_changed = asyncio.Event()
        self._closed = False
        self.transport = ReverseConnectionTransport(self)
        self.huawei_client = AsyncHuaweiSolarClient(self.transport, unit_id=0)
        self.clients_by_role: dict[str, AsyncHuaweiSolarClient] = {
            "emma": self.huawei_client
        }
        self.devices_by_role: dict[str, Any] = {}

    def set_subscriptions(self, register_names: set[str]) -> None:
        """Replace the active poll set and wake the scheduler immediately."""
        register_names = expand_subscriptions(set(register_names))
        added = register_names - self.subscribed_registers
        removed = self.subscribed_registers - register_names
        self.subscribed_registers = set(register_names)
        self.unsupported_registers.intersection_update(register_names)
        self.unsupported_registers.difference_update(added)
        for register_name in removed:
            self.latest_states.pop(register_name, None)
            self.latest_values.pop(register_name, None)
            self.latest_updated_at.pop(register_name, None)
        self._subscriptions_changed.set()
        log.info(
            "Polling subscriptions updated total=%d added=%d removed=%d",
            len(register_names),
            len(added),
            len(removed),
        )
        if added:
            log.info("Polling subscriptions added: %s", ", ".join(sorted(added)))
        if removed:
            log.info("Polling subscriptions removed: %s", ", ".join(sorted(removed)))
        for group, group_registers in POLL_GROUPS.items():
            log.info(
                "Polling subscription group=%s active=%d",
                group,
                len(register_names.intersection(group_registers)),
            )

    @property
    def closed(self) -> bool:
        return self._closed or self.writer.is_closing()

    async def run(self) -> None:
        master_task = asyncio.create_task(self._master_loop(), name=f"master-{self.peer}")
        try:
            while True:
                frame = await read_modbus_frame(self.reader)
                self._log_frame("RX EMMA -> server", frame)
                self._dispatch(frame)
        except asyncio.IncompleteReadError:
            pass
        except (ConnectionError, ModbusProtocolError) as error:
            log.warning("Connection %s ended with protocol error: %s", self.peer, error)
        finally:
            self._closed = True
            master_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await master_task
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("EMMA connection closed"))
            self._pending.clear()
            self._request_pdus.clear()
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()

    def _dispatch(self, frame: ModbusFrame) -> None:
        future = self._pending.pop(frame.transaction_id, None)
        if future is not None:
            if not future.done():
                future.set_result(frame)
            return

        if frame.function_code == 0x41:
            info = parse_huawei_device_info(frame.pdu)
            if info:
                self.device_info = info
                log.info(
                    "Huawei startup/device frame model=%s firmware=%s serial=%s "
                    "protocol=%s product_type=%s",
                    info.get("1", "-"),
                    info.get("2", "-"),
                    info.get("4", "-"),
                    info.get("3", "-"),
                    info.get("8", "-"),
                )
            else:
                log.info("Unsolicited Huawei-defined frame: %s", describe_frame(frame))
            self._startup_frame.set()
            return

        log.warning("Ignoring unsolicited Modbus frame: %s", describe_frame(frame))

    async def request(self, unit_id: int, pdu: bytes) -> ModbusFrame:
        if self.read_only and pdu and pdu[0] in MODBUS_WRITE_FUNCTION_CODES:
            log.warning(
                "Blocked Modbus write fc=0x%02X unit=%d: read-only mode", pdu[0], unit_id
            )
            raise ReadOnlyModeError()
        if self.closed:
            raise ConnectionError("EMMA connection is closed")

        async with self._request_lock:
            transaction_id = self._allocate_transaction_id()
            future = asyncio.get_running_loop().create_future()
            self._pending[transaction_id] = future
            self._request_pdus[transaction_id] = pdu
            frame = ModbusFrame(transaction_id, 0, unit_id, pdu)
            raw = frame.encode()
            self._log_frame("TX server -> EMMA", frame, raw)
            self.writer.write(raw)
            await self.writer.drain()

            try:
                response = await asyncio.wait_for(future, timeout=self.request_timeout)
            except BaseException:
                self._pending.pop(transaction_id, None)
                self._request_pdus.pop(transaction_id, None)
                raise

        self._request_pdus.pop(transaction_id, None)
        if not response.pdu:
            raise ModbusProtocolError("Received an empty Modbus response PDU")
        if response.protocol_id != 0:
            raise ModbusProtocolError(
                f"Received unsupported MBAP protocol ID {response.protocol_id}"
            )
        if response.pdu[0] & 0x80:
            exception_code = response.pdu[1] if len(response.pdu) > 1 else 0
            raise ModbusExceptionResponse(response.pdu[0], exception_code)
        return response

    def _allocate_transaction_id(self) -> int:
        for _ in range(0xFFFF):
            transaction_id = self._next_transaction_id
            self._next_transaction_id = 1 if transaction_id == 0xFFFF else transaction_id + 1
            if transaction_id not in self._pending:
                return transaction_id
        raise RuntimeError("No free Modbus transaction IDs")

    async def read_holding_registers(self, unit_id: int, address: int, count: int) -> list[int]:
        if not 0 <= unit_id <= 0xFF:
            raise ValueError("Unit ID must be between 0 and 255")
        if not 0 <= address <= 0xFFFF:
            raise ValueError("Register address must be between 0 and 65535")
        if not 1 <= count <= MAX_READ_REGISTERS:
            raise ValueError(f"Register count must be between 1 and {MAX_READ_REGISTERS}")

        response = await self.request(unit_id, struct.pack(">BHH", 0x03, address, count))
        expected_byte_count = count * 2
        if response.pdu[0] != 0x03 or len(response.pdu) != 2 + expected_byte_count:
            raise ModbusProtocolError(
                f"Invalid read response for {address}/{count}: {response.pdu.hex(' ')}"
            )
        if response.pdu[1] != expected_byte_count:
            raise ModbusProtocolError(
                f"Read response byte count is {response.pdu[1]}, expected {expected_byte_count}"
            )
        return list(struct.unpack(f">{count}H", response.pdu[2:]))

    async def write_single_register(self, unit_id: int, address: int, value: int) -> None:
        if not 0 <= value <= 0xFFFF:
            raise ValueError("Register value must be between 0 and 65535")
        request_pdu = struct.pack(">BHH", 0x06, address, value)
        response = await self.request(unit_id, request_pdu)
        if response.pdu != request_pdu:
            raise ModbusProtocolError(
                f"Invalid write-single response: {response.pdu.hex(' ')}"
            )

    async def write_multiple_registers(
        self, unit_id: int, address: int, values: list[int]
    ) -> None:
        if not 1 <= len(values) <= MAX_WRITE_REGISTERS:
            raise ValueError(
                f"Write-multiple requires between 1 and {MAX_WRITE_REGISTERS} registers"
            )
        if any(not 0 <= value <= 0xFFFF for value in values):
            raise ValueError("Register values must be between 0 and 65535")

        data = struct.pack(f">{len(values)}H", *values)
        request_pdu = struct.pack(">BHHB", 0x10, address, len(values), len(data)) + data
        response = await self.request(unit_id, request_pdu)
        expected = struct.pack(">BHH", 0x10, address, len(values))
        if response.pdu != expected:
            raise ModbusProtocolError(
                f"Invalid write-multiple response: {response.pdu.hex(' ')}"
            )

    async def read_device_list(self) -> dict[int, bytes]:
        objects: dict[int, bytes] = {}
        requested_object = 0x87
        seen: set[int] = set()
        for _ in range(32):
            if requested_object in seen:
                raise ModbusProtocolError("Device-list pagination loop detected")
            seen.add(requested_object)
            response = await self.request(
                0, bytes((0x2B, 0x0E, 0x03, requested_object))
            )
            page = parse_device_identification_page(response.pdu)
            objects.update(page.objects)
            if not page.more_follows:
                self.device_objects = objects
                return objects
            requested_object = page.next_object_id
        raise ModbusProtocolError("Device-list response exceeded 32 pages")

    def capability(self, name: str) -> bool | None:
        """Return a feature-mask capability: True/False when known, else None."""
        return capability_state(name, self.latest_values)

    def capabilities(self) -> dict[str, bool | None]:
        return capability_states(self.latest_values)

    async def set_register_value(self, register_name: str, value: Any) -> Any:
        """Validate, write and verify one allowlisted register.

        Success is reported only after a readback from the device matches the
        requested value. A TOU schedule identical to a fresh readback is not
        rewritten, which keeps EMMA flash writes idempotent.
        """
        if self.read_only:
            raise ReadOnlyModeError()
        description = ENTITY_CATALOG.get(register_name)
        if description is None or not description.writeable:
            raise ValueError(f"Register is unknown or read-only: {register_name}")
        definition = REGISTERS[register_name]
        converted = _convert_register_value(description, definition.unit, value)
        for capability in description.required_capabilities:
            if self.capability(capability) is False:
                raise ValueError(
                    f"{register_name} is not supported by the connected hardware "
                    f"(feature mask capability {capability} is not present)"
                )
        if register_name in INVERTER_RATED_LIMITED_REGISTERS:
            rated_power = self.latest_values.get(str(rn.INVERTER_RATED_POWER))
            if rated_power is not None and float(converted) > float(rated_power):
                raise ValueError(
                    f"Value cannot exceed the inverter rated power ({rated_power} W)"
                )
        role = description.client_role
        client = self.clients_by_role.get(role)
        device = self.devices_by_role.get(role)
        if client is None:
            raise ValueError(f"No connected {role} device is available")
        reader = device if device is not None else client
        expected = normalize_register_value(register_name, converted)

        if description.format == "tou_periods":
            current = normalize_register_value(
                register_name, (await reader.get(register_name)).value
            )
            if tou_codec.schedules_equal(current, expected):
                log.info(
                    "TOU schedule unchanged (%d periods); skipping EMMA write",
                    len(expected),
                )
                return self._record_verified_write(register_name, current, False)

        await reader.set(register_name, converted)
        normalized: Any = None
        for attempt in range(2):
            decoded = await reader.get(register_name)
            normalized = normalize_register_value(register_name, decoded.value)
            if _readback_matches(description, expected, normalized):
                return self._record_verified_write(register_name, normalized, True)
            if attempt == 0:
                await asyncio.sleep(1.0)
        # Keep the real readback visible so consumers never see the requested
        # value as if it had been applied.
        self.latest_values[register_name] = normalized
        self.latest_updated_at[register_name] = _now_iso()
        raise WriteVerificationError(
            f"Readback mismatch for {register_name}: requested {expected!r}, "
            f"device reports {normalized!r}"
        )

    def _record_verified_write(
        self, register_name: str, normalized: Any, written: bool
    ) -> Any:
        timestamp = _now_iso()
        self.latest_values[register_name] = normalized
        self.latest_updated_at[register_name] = timestamp
        self.last_verified_write = {
            "register_name": register_name,
            "verified_at": timestamp,
            "written": written,
        }
        return normalized

    async def set_tou_periods(self, periods: Any) -> Any:
        return await self.set_register_value(str(rn.EMMA_TOU_PERIODS), periods)

    async def execute_command(self, name: str, value: Any) -> None:
        if self.read_only:
            raise ReadOnlyModeError()
        if name == "ess_control_mode":
            integer = _require_integer(value, {2, 4, 5, 6})
            await self.huawei_client.set(
                rn.EMMA_ESS_CONTROL_MODE,
                rv.EmmaEssControlMode(integer),
            )
        elif name == "preferred_surplus_pv_use":
            integer = _require_integer(value, {0, 1})
            await self.huawei_client.set(
                rn.EMMA_TOU_PREFERRED_USE_OF_SURPLUS_PV_POWER,
                rv.StorageExcessPvEnergyUseInTOU(integer),
            )
        elif name == "max_grid_charge_power_kw":
            number = _require_number(value, minimum=0, maximum=50)
            await self.huawei_client.set(
                rn.EMMA_TOU_MAXIMUM_POWER_FOR_CHARGING_BATTERIES_FROM_GRID,
                round(number * 1000),
            )
        elif name == "limited_feed_in_mode":
            integer = _require_integer(value, {0, 5, 6, 7})
            await self.huawei_client.set(
                rn.EMMA_POWER_CONTROL_MODE_AT_GRID_CONNECTION_POINT,
                rv.ActivePowerControlMode(integer),
            )
        elif name == "max_grid_feed_in_kw":
            rated = self.latest_states.get(
                "sensor.huawei_emma_inverter_rated_power", {}
            ).get("state")
            maximum = float(rated) if rated is not None else 100.0
            number = _require_number(value, minimum=-1, maximum=maximum)
            await self.huawei_client.set(
                rn.EMMA_MAXIMUM_FEED_GRID_POWER_WATT,
                round(number * 1000),
            )
        elif name == "max_grid_feed_in_percent":
            number = _require_number(value, minimum=0, maximum=100)
            await self.huawei_client.set(
                rn.EMMA_MAXIMUM_FEED_GRID_POWER_PERCENT,
                number,
            )
        else:
            raise ValueError(f"Unknown or disallowed command: {name}")

    async def _master_loop(self) -> None:
        try:
            await asyncio.wait_for(self._startup_frame.wait(), timeout=3.0)
        except TimeoutError:
            log.info("No Huawei startup frame within 3 seconds; starting master probe")

        try:
            objects = await self.read_device_list()
            self.topology_devices = parse_topology_devices(objects)
            self.available_device_roles = {
                device["role"] for device in self.topology_devices
            }
            self.topology_complete = bool(self.topology_devices)
            descriptions = {
                f"0x{object_id:02X}": value.decode("ascii", errors="replace")
                for object_id, value in objects.items()
            }
            log.info("Device-list objects: %s", descriptions)
            log.info(
                "Discovered topology: %s",
                [
                    f"{device['role']}:{device['model']}:{device.get('serial_number') or '-'}"
                    for device in self.topology_devices
                ],
            )
        except (TimeoutError, ModbusProtocolError, ConnectionError) as error:
            log.warning("Device-list probe failed; continuing with register polling: %s", error)

        try:
            self.device = await create_device_instance(self.huawei_client)
            _use_raw_register_values(self.device)
            self.devices_by_role["emma"] = self.device
            self.device_details["emma"] = {
                "unit_id": 0,
                "model": self.device.model_name,
                "serial_number": getattr(self.device, "serial_number", None),
                "has_battery": getattr(self.device, "has_battery", None),
                "battery_type": json_value(getattr(self.device, "battery_type", None)),
                "supports_capacity_control": getattr(
                    self.device, "supports_capacity_control", None
                ),
                "supports_power_limited_grid_connection": getattr(
                    self.device, "supports_power_limited_grid_connection", None
                ),
            }
            self.device_info.setdefault("1", self.device.model_name)
            if getattr(self.device, "software_version", None):
                self.device_info.setdefault("2", self.device.software_version)
            if getattr(self.device, "serial_number", None):
                self.device_info.setdefault("4", self.device.serial_number)
            log.info(
                "Initialized huawei-solar device model=%s serial=%s firmware=%s",
                self.device.model_name,
                getattr(self.device, "serial_number", "-"),
                getattr(self.device, "software_version", "-"),
            )
        except (TimeoutError, ConnectionError, HuaweiSolarException) as error:
            log.warning("huawei-solar device discovery failed; using generic client: %s", error)

        await self._initialize_subdevices()

        next_due = {name: 0.0 for name in POLL_GROUPS}
        while True:
            now = asyncio.get_running_loop().time()
            due_groups = [name for name, due in next_due.items() if due <= now]
            cycle_error: Exception | None = None
            # Publish each group as soon as it finishes. In particular, this makes
            # fast/core values available to Home Assistant while the longer first
            # medium and slow scans are still running.
            for group in due_groups:
                names = [
                    register_name
                    for register_name in POLL_GROUPS[group]
                    if register_name in self.subscribed_registers
                    if register_name not in self.unsupported_registers
                    and register_name not in self.not_applicable_registers
                    and self._register_available_by_topology(register_name)
                ]
                stats = self.poll_stats[group]
                started = asyncio.get_running_loop().time()
                stats["runs"] += 1
                stats["registers_requested"] = len(names)
                try:
                    decoded: dict[str, Result[Any]] = {}
                    names_by_role: dict[str, list[str]] = {}
                    for register_name in names:
                        names_by_role.setdefault(
                            ENTITY_CATALOG[register_name].client_role, []
                        ).append(register_name)
                    for role_names in names_by_role.values():
                        decoded.update(
                            await self._read_registers_with_fallback(role_names)
                        )
                    states, values = build_catalog_states(decoded, self.latest_values)
                    timestamp = _now_iso()
                    stats["last_success"] = timestamp
                    stats["registers_updated"] = len(values)
                    stats["last_error"] = None
                    self.latest_states.update(states)
                    self.latest_values.update(values)
                    self.latest_updated_at.update(
                        {name: timestamp for name in values}
                    )
                    self.last_poll_success = timestamp
                    await self.state_callback(self, self.latest_states)
                    log.info(
                        "EMMA poll group=%s registers=%d updated=%d unsupported=%d",
                        group,
                        len(names),
                        len(values),
                        len(self.unsupported_registers),
                    )
                    if self.publisher is not None:
                        await self.publisher.publish(states)
                except (
                    TimeoutError,
                    ModbusProtocolError,
                    ConnectionError,
                    HuaweiSolarException,
                ) as error:
                    cycle_error = error
                    self.last_poll_error = str(error)
                    stats["failures"] += 1
                    stats["last_error"] = str(error)
                    log.warning("EMMA poll group=%s failed: %s", group, error)
                finally:
                    stats["last_duration_ms"] = round(
                        (asyncio.get_running_loop().time() - started) * 1000
                    )

            if cycle_error is None:
                self.last_poll_error = None

            for group in due_groups:
                next_due[group] = now + self.poll_intervals[group]

            if self.once:
                self.writer.close()
                return
            delay = max(0.1, min(next_due.values()) - asyncio.get_running_loop().time())
            try:
                await asyncio.wait_for(self._subscriptions_changed.wait(), timeout=delay)
            except TimeoutError:
                pass
            else:
                self._subscriptions_changed.clear()
                next_due = {name: 0.0 for name in POLL_GROUPS}

    async def _read_registers_with_fallback(
        self, names: list[str]
    ) -> dict[str, Result[Any]]:
        if not names:
            return {}
        structured = [
            name for name in names if ENTITY_CATALOG[name].format is not None
        ]
        if structured and len(names) > 1:
            result = await self._read_registers_with_fallback(
                [name for name in names if name not in structured]
            )
            for register_name in structured:
                result.update(
                    await self._read_registers_with_fallback([register_name])
                )
            return result
        role = ENTITY_CATALOG[names[0]].client_role
        client = self.clients_by_role.get(role)
        device = self.devices_by_role.get(role)
        if client is None:
            self.unsupported_registers.update(names)
            return {}
        try:
            if len(names) == 1 and ENTITY_CATALOG[names[0]].format is not None:
                description = ENTITY_CATALOG[names[0]]
                log.info(
                    "Reading structured register %s unit=%d address=%d (0x%04X) count=%d",
                    names[0],
                    client.unit_id,
                    description.address,
                    description.address,
                    description.length,
                )
                decoded = await (device.get(names[0]) if device else client.get(names[0]))
                value = json_value(decoded.value)
                log.info(
                    "Decoded structured register %s periods=%d",
                    names[0],
                    len(value) if isinstance(value, list) else -1,
                )
                log.debug("Decoded structured register %s value=%s", names[0], value)
                return {names[0]: decoded}
            if device is not None:
                return await device.batch_update(names)
            result: dict[str, Result[Any]] = {}
            for register_name in names:
                result[register_name] = await client.get(register_name)
            return result
        except DecodeError as error:
            if len(names) > 1:
                midpoint = len(names) // 2
                first = await self._read_registers_with_fallback(names[:midpoint])
                second = await self._read_registers_with_fallback(names[midpoint:])
                return {**first, **second}
            return await self._read_raw_fallback(names[0], client.unit_id, error)
        except ReadException as error:
            if structured:
                log.warning(
                    "Structured register read failed %s address=%d count=%d: %s",
                    names[0],
                    ENTITY_CATALOG[names[0]].address,
                    ENTITY_CATALOG[names[0]].length,
                    error,
                )
            if error.modbus_exception_code not in (2, 3):
                raise
            if len(names) == 1:
                register_name = names[0]
                self.unsupported_registers.add(register_name)
                log.warning("Register %s is unsupported by this EMMA: %s", register_name, error)
                return {}
            midpoint = len(names) // 2
            first = await self._read_registers_with_fallback(names[:midpoint])
            second = await self._read_registers_with_fallback(names[midpoint:])
            return {**first, **second}

    async def _initialize_subdevices(self) -> None:
        """Create clients for downstream devices reported in the device list."""
        for topology_device in self.topology_devices:
            role = topology_device["role"]
            unit_id = topology_device.get("unit_id")
            if role not in SUBDEVICE_ROLES or not isinstance(unit_id, int):
                continue
            if role in self.clients_by_role:
                log.warning(
                    "Multiple %s devices reported; using unit %d and ignoring unit %d",
                    role,
                    self.clients_by_role[role].unit_id,
                    unit_id,
                )
                continue
            client = AsyncHuaweiSolarClient(self.transport, unit_id=unit_id)
            self.clients_by_role[role] = client
            details: dict[str, Any] = {
                "unit_id": unit_id,
                "model": topology_device.get("model"),
                "serial_number": topology_device.get("serial_number"),
            }
            self.device_details[role] = details
            try:
                device = await create_device_instance(client)
                _use_raw_register_values(device)
                self.devices_by_role[role] = device
                details["model"] = device.model_name or details["model"]
                log.info(
                    "Initialized %s subdevice unit=%d model=%s serial=%s",
                    role,
                    unit_id,
                    device.model_name,
                    getattr(device, "serial_number", topology_device.get("serial_number") or "-"),
                )
            except (TimeoutError, ConnectionError, HuaweiSolarException) as error:
                device = None
                log.warning(
                    "Could not initialize %s subdevice unit=%d; individual register access will be used: %s",
                    role,
                    unit_id,
                    error,
                )
            if role == "smartguard":
                self._apply_smartguard_hardware(device, details)

    def _apply_smartguard_hardware(self, device: Any, details: dict[str, Any]) -> None:
        """Record SmartGuard identity and filter phase registers by hardware."""
        phase_type = getattr(device, "phase_type", None) if device else None
        details["phase_type"] = json_value(phase_type)
        details["software_version"] = getattr(device, "software_version", None) if device else None
        details["supports_grid_switchover"] = (
            bool(getattr(device, "supports_grid_switchover", False)) if device else None
        )
        if phase_type is not None and json_value(phase_type) == "single_phase":
            filtered = {
                name
                for name, description in ENTITY_CATALOG.items()
                if description.three_phase_only
            }
            self.not_applicable_registers.update(filtered)
            log.info(
                "SmartGuard is single-phase; not polling %d phase-specific registers",
                len(filtered),
            )

    async def _read_raw_fallback(
        self, register_name: str, unit_id: int, error: Exception
    ) -> dict[str, Result[Any]]:
        """Publish the raw integer of a register that upstream cannot decode.

        An enum value unknown to huawei-solar must not invalidate the rest of
        its batch, and must not be guessed. ESPHome publishes the same raw
        number for an unmapped enum value.
        """
        description = ENTITY_CATALOG[register_name]
        definition = REGISTERS[register_name]
        if description.length not in (1, 2) or description.value_kind not in (
            "enum",
            "map",
            "number",
        ):
            log.warning("Register %s could not be decoded: %s", register_name, error)
            self.unsupported_registers.add(register_name)
            return {}
        registers = await self.read_holding_registers(
            unit_id, description.address, description.length
        )
        raw = registers[0] if description.length == 1 else registers[0] << 16 | registers[1]
        if type(definition).__name__.startswith("I"):
            bits = 16 * description.length
            if raw >= 1 << (bits - 1):
                raw -= 1 << bits
        if register_name not in self.raw_value_registers:
            log.warning(
                "Register %s returned undecodable value %d; publishing raw value: %s",
                register_name,
                raw,
                error,
            )
        self.raw_value_registers.add(register_name)
        return {register_name: Result(raw, None)}

    def _register_available_by_topology(self, register_name: str) -> bool:
        if not self.topology_complete:
            return True
        description = ENTITY_CATALOG[register_name]
        role = (
            description.device_role
            if description.device_role == "external_meter"
            else description.client_role
        )
        if role != "emma" and role not in self.available_device_roles:
            if register_name not in self.unsupported_registers:
                self.unsupported_registers.add(register_name)
                log.info(
                    "Skipping %s because no %s was reported in the device list",
                    register_name,
                    role,
                )
            return False
        return True

    def _log_frame(self, direction: str, frame: ModbusFrame, raw: bytes | None = None) -> None:
        request_pdu = (
            self._request_pdus.get(frame.transaction_id)
            if direction.startswith("RX")
            else None
        )
        log.info("%s %s", direction, describe_frame(frame, request_pdu))
        if self.log_raw:
            packet = raw if raw is not None else frame.encode()
            log.info("%s raw=%s", direction, packet.hex(" ").upper())


def default_subscriptions() -> set[str]:
    """Registers polled before Home Assistant sends its subscription set."""
    return {
        name for name, description in ENTITY_CATALOG.items() if description.enabled_default
    }


def _readback_matches(description: EntityDescription, expected: Any, actual: Any) -> bool:
    """Compare a requested value with the device readback after a write."""
    if description.format == "tou_periods":
        return tou_codec.schedules_equal(expected, actual)
    if isinstance(expected, bool) or isinstance(actual, bool) or isinstance(expected, str):
        return expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        gain = getattr(REGISTERS[description.register_name], "gain", 1) or 1
        tolerance = 1 / float(gain) + 1e-9
        if description.register_name == str(rn.EMMA_SYSTEM_TIME):
            tolerance = 30  # the clock keeps running between write and readback
        return abs(float(expected) - float(actual)) <= tolerance
    return expected == actual


def _require_integer(value: Any, allowed: set[int]) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid command value")
    try:
        integer = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Command value must be an integer") from error
    if integer != value or integer not in allowed:
        raise ValueError(f"Command value must be one of {sorted(allowed)}")
    return integer


def _require_number(value: Any, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid command value")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Command value must be numeric") from error
    if not minimum <= number <= maximum:
        raise ValueError(f"Command value must be between {minimum} and {maximum}")
    return number


def _convert_register_value(
    description: EntityDescription, unit: Any, value: Any
) -> Any:
    if description.format == "tou_periods":
        try:
            periods = tou_codec.normalize_periods(value)
        except tou_codec.TouValidationError as error:
            raise ValueError(str(error)) from error
        return [
            HUAWEI_LUNA2000_TimeOfUsePeriod(
                start_time=period["start_time"],
                end_time=period["end_time"],
                charge_flag=(
                    ChargeFlag.CHARGE
                    if period["action"] == tou_codec.CHARGE
                    else ChargeFlag.DISCHARGE
                ),
                days_effective=tuple(period["days_effective"]),
            )
            for period in periods
        ]

    if isinstance(unit, type) and issubclass(unit, IntEnum):
        member = _enum_member(description, unit, value)
        if description.allowed_values and int(member) not in description.allowed_values:
            allowed = [
                str(option["label"])
                for option in description.options
                if option["value"] in description.allowed_values
            ]
            raise ValueError(
                f"{member.name.lower()} is not an allowed value for "
                f"{description.register_name}; use one of: {', '.join(allowed)}"
            )
        return member

    if unit is bool:
        if not isinstance(value, bool):
            raise ValueError("Value must be true or false")
        return value

    if description.minimum is not None and description.maximum is not None:
        number = _require_number(
            value, minimum=description.minimum, maximum=description.maximum
        )
        return int(number) if description.step == 1 else number

    raise ValueError(f"Writing {description.register_name} is not exposed safely")


def _enum_member(description: EntityDescription, unit: Any, value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("Invalid enum value")
    if isinstance(value, str):
        normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
        for option in description.options:
            label = str(option["label"]).lower().replace(" ", "_")
            if normalized in (label, option["key"]):
                return unit(option["value"])
        try:
            return unit[value.strip().upper()]
        except KeyError as error:
            raise ValueError(
                f"Value must be one of: {', '.join(str(x['label']) for x in description.options)}"
            ) from error
    try:
        return unit(int(value))
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid enum value") from error


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class RuntimeState:
    connector_instance_id: str = field(default_factory=lambda: secrets.token_hex(8))
    active_session: ReverseModbusSession | None = None
    latest_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_values: dict[str, Any] = field(default_factory=dict)
    updated_at: dict[str, str] = field(default_factory=dict)
    connected_at: str | None = None
    last_success: str | None = None
    last_error: str | None = None
    reconnect_count: int = 0
    subscribed_registers: set[str] = field(
        default_factory=lambda: expand_subscriptions(default_subscriptions())
    )
    read_only: bool = False

    def set_read_only(self, enabled: bool) -> None:
        """Switch read-only mode without reconnecting or losing subscriptions."""
        self.read_only = bool(enabled)
        if self.active_session is not None:
            self.active_session.read_only = self.read_only
        log.info("EMMA read-only mode %s", "enabled" if self.read_only else "disabled")

    async def update_states(
        self,
        session: ReverseModbusSession,
        states: dict[str, dict[str, Any]],
    ) -> None:
        if session is self.active_session:
            self.latest_states = states
            self.latest_values = dict(session.latest_values)
            self.updated_at = dict(session.latest_updated_at)
            self.last_success = _now_iso()
            self.last_error = None

    def health(self) -> dict[str, Any]:
        session = self.active_session
        return {
            "connector_instance_id": self.connector_instance_id,
            "connected": session is not None and not session.closed,
            "peer": session.peer if session is not None else None,
            "connected_at": self.connected_at,
            "last_success": session.last_poll_success if session else self.last_success,
            "last_error": session.last_poll_error if session else self.last_error,
            "reconnect_count": self.reconnect_count,
            "registers_available": len(self.latest_values),
            "registers_unsupported": len(session.unsupported_registers) if session else 0,
            "registers_subscribed": len(self.subscribed_registers),
            "registers_not_applicable": (
                len(session.not_applicable_registers) if session else 0
            ),
            "registers_stale": len(self.stale_registers()),
            "catalog_sha256": CATALOG_SHA256,
            "runtime": "python",
            "read_only": self.read_only,
        }

    def stale_registers(self) -> list[str]:
        """Return values older than three poll intervals of their group.

        Values are kept for diagnostics, but consumers must not present them as
        live telemetry.
        """
        session = self.active_session
        intervals = (
            session.poll_intervals if session is not None else dict(POLL_INTERVALS)
        )
        now = datetime.now(timezone.utc)
        stale: list[str] = []
        for register_name, updated in self.updated_at.items():
            description = ENTITY_CATALOG.get(register_name)
            if description is None:
                continue
            try:
                age = (now - datetime.fromisoformat(updated)).total_seconds()
            except (TypeError, ValueError):
                continue
            if age > 3 * float(intervals.get(description.poll_group, 300)) + 60:
                stale.append(register_name)
        return sorted(stale)

    def diagnostics(self) -> dict[str, Any]:
        """Return connector diagnostics for support and Home Assistant."""
        session = self.active_session
        subscriptions: dict[str, dict[str, int]] = {}
        for register_name in self.subscribed_registers:
            description = ENTITY_CATALOG[register_name]
            role = subscriptions.setdefault(description.client_role, {})
            role[description.poll_group] = role.get(description.poll_group, 0) + 1
        return {
            "runtime": "python",
            "connector_version": CONNECTOR_VERSION,
            "huawei_solar_version": _package_version("huawei-solar"),
            "tmodbus_version": _package_version("tmodbus"),
            "catalog_sha256": CATALOG_SHA256,
            "catalog_entities": len(ENTITY_CATALOG),
            "connected": session is not None and not session.closed,
            "devices": session.device_details if session else {},
            "read_only": self.read_only,
            "control_available": not self.read_only,
            "capabilities": {
                **(session.capabilities() if session else {}),
                "writes": not self.read_only,
            },
            "control_registers_unavailable": (
                sorted(
                    name
                    for name in session.unsupported_registers
                    if ENTITY_CATALOG[name].writeable
                )
                if session
                else []
            ),
            "subscriptions": subscriptions,
            "registers_subscribed": len(self.subscribed_registers),
            "registers_unsupported": (
                sorted(session.unsupported_registers) if session else []
            ),
            "registers_not_applicable": (
                sorted(session.not_applicable_registers) if session else []
            ),
            "registers_raw_values": sorted(session.raw_value_registers) if session else [],
            "registers_stale": self.stale_registers(),
            "poll_groups": session.poll_stats if session else {},
            "last_poll_success": session.last_poll_success if session else self.last_success,
            "last_verified_write": session.last_verified_write if session else None,
        }

    def device(self) -> dict[str, Any]:
        session = self.active_session
        startup = session.device_info if session is not None else {}
        objects = session.device_objects if session is not None else {}
        devices = session.topology_devices if session is not None else []
        return {
            "manufacturer": "Huawei",
            "model": startup.get("1", "EMMA"),
            "serial_number": startup.get("4"),
            "sw_version": startup.get("2"),
            "protocol_version": startup.get("3"),
            "product_type": startup.get("8"),
            "startup": startup,
            "devices": devices,
            "topology": {
                f"0x{object_id:02X}": value.decode("ascii", errors="replace")
                for object_id, value in objects.items()
            },
        }

    def entities(self) -> list[dict[str, Any]]:
        """Return every known entity; unsupported devices remain unavailable."""
        descriptions: list[dict[str, Any]] = []
        for description in ENTITY_CATALOG.values():
            payload = description.to_dict()
            if description.register_name in INVERTER_RATED_LIMITED_REGISTERS:
                rated_power = self.latest_values.get(str(rn.INVERTER_RATED_POWER))
                if rated_power is not None:
                    payload["maximum"] = float(rated_power)
            descriptions.append(payload)
        return descriptions

    def set_subscriptions(self, register_names: list[str]) -> list[str]:
        """Validate and replace the set of registers polled by the connector."""
        if not isinstance(register_names, list) or any(
            not isinstance(name, str) for name in register_names
        ):
            raise ValueError("register_names must be a list of strings")
        unknown = sorted(set(register_names) - set(ENTITY_CATALOG))
        if unknown:
            raise ValueError(f"Unknown register names: {', '.join(unknown[:10])}")
        subscriptions = expand_subscriptions(set(register_names))
        removed = self.subscribed_registers - subscriptions
        self.subscribed_registers = subscriptions
        for register_name in removed:
            self.latest_states.pop(register_name, None)
            self.latest_values.pop(register_name, None)
            self.updated_at.pop(register_name, None)
        session = self.active_session
        if session is not None and not session.closed:
            session.set_subscriptions(subscriptions)
        return sorted(subscriptions)

    def states(self) -> dict[str, Any]:
        """Return a serializable snapshot matching the external connector API."""
        session = self.active_session
        return {
            "values": self.latest_values,
            "updated_at": self.updated_at,
            "unsupported": sorted(session.unsupported_registers) if session else [],
            "not_applicable": sorted(session.not_applicable_registers) if session else [],
            "stale": self.stale_registers(),
        }

    def connected_session(self) -> ReverseModbusSession:
        """Return the active EMMA session or fail clearly."""
        session = self.active_session
        if session is None or session.closed:
            raise ConnectionError("EMMA is not connected")
        return session

    async def set_value(self, register_name: str, value: Any) -> Any:
        """Validate, write and cache a register value."""
        if self.read_only:
            raise ReadOnlyModeError()
        session = self.connected_session()
        result = await session.set_register_value(register_name, value)
        self.latest_values[register_name] = result
        self.updated_at[register_name] = session.latest_updated_at[register_name]
        return result

    async def set_tou_periods(self, periods: Any) -> Any:
        """Validate, write and cache the full EMMA TOU schedule."""
        if self.read_only:
            raise ReadOnlyModeError()
        session = self.connected_session()
        result = await session.set_tou_periods(periods)
        register_name = str(rn.EMMA_TOU_PERIODS)
        self.latest_values[register_name] = result
        self.updated_at[register_name] = session.latest_updated_at[register_name]
        return result


@dataclass(slots=True)
class ServerConfig:
    """Runtime settings for a managed reverse Modbus/TLS listener."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    certfile: Path = DEFAULT_CERTFILE
    keyfile: Path = DEFAULT_KEYFILE
    ca_certfile: Path = DEFAULT_CA_CERTFILE
    ca_keyfile: Path = DEFAULT_CA_KEYFILE
    cert_name: str | None = None
    cafile: Path | None = None
    password: str | None = None
    fast_interval: float = 30.0
    medium_interval: float = 300.0
    slow_interval: float = 1800.0
    request_timeout: float = 5.0
    log_raw: bool = False
    once: bool = False
    generate_certificates: bool = True
    read_only: bool = False


class ReverseModbusTlsServer:
    """Lifecycle-managed server suitable for a Home Assistant config entry."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.state = RuntimeState(read_only=config.read_only)
        self.certificate_status: CertificateStatus | None = None
        self.days_remaining: int | None = None
        self._server: asyncio.Server | None = None
        self._handlers: set[asyncio.Task[Any]] = set()

    @property
    def running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def async_start(self) -> None:
        if self.running:
            return
        self.certificate_status = await asyncio.to_thread(
            ensure_certificates,
            self.config.certfile,
            self.config.keyfile,
            self.config.ca_certfile,
            self.config.ca_keyfile,
            self.config.cert_name,
            self.config.password,
            self.config.generate_certificates,
        )
        self.days_remaining = await asyncio.to_thread(
            certificate_days_remaining, self.config.certfile
        )
        status = self.certificate_status
        if status.created_ca:
            log.warning(
                "Created local TLS CA at %s (SHA-256 %s). Import this CA in "
                "EMMA's third-party management trust settings.",
                self.config.ca_certfile,
                status.fingerprint,
            )
        if status.created_server:
            log.info("Created/renewed TLS server certificate at %s", self.config.certfile)
        log.info(
            "TLS certificate validation passed; %d days remaining",
            self.days_remaining,
        )
        if self.days_remaining < 30:
            log.warning("TLS server certificate expires in %d days", self.days_remaining)

        ssl_context = build_ssl_context(self.config)
        self._server = await asyncio.start_server(
            self._handle_emma,
            self.config.host,
            self.config.port,
            ssl=ssl_context,
        )
        addresses = ", ".join(
            str(sock.getsockname()) for sock in self._server.sockets or []
        )
        log.info(
            "Huawei reverse Modbus/TLS listener ready on %s using cert=%s key=%s",
            addresses,
            self.config.certfile,
            self.config.keyfile,
        )

    async def _handle_emma(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        peer = writer.get_extra_info("peername")
        try:
            if (
                self.state.active_session is not None
                and not self.state.active_session.closed
            ):
                log.warning("Rejecting additional EMMA connection from %s", peer)
                writer.close()
                await writer.wait_closed()
                return
            session = ReverseModbusSession(
                reader,
                writer,
                request_timeout=self.config.request_timeout,
                poll_interval=self.config.fast_interval,
                poll_intervals={
                    "fast": self.config.fast_interval,
                    "medium": self.config.medium_interval,
                    "slow": self.config.slow_interval,
                },
                publisher=None,
                state_callback=self.state.update_states,
                subscribed_registers=self.state.subscribed_registers,
                log_raw=self.config.log_raw,
                once=self.config.once,
                read_only=self.state.read_only,
            )
            self.state.active_session = session
            self.state.connected_at = _now_iso()
            self.state.reconnect_count += 1
            log.info(
                "TLS client connected from %s; management system is now Modbus master",
                peer,
            )
            try:
                await session.run()
            finally:
                if self.state.active_session is session:
                    self.state.active_session = None
                log.info("TLS client disconnected from %s", peer)
        finally:
            if task is not None:
                self._handlers.discard(task)

    async def async_stop(self) -> None:
        """Stop accepting connections and close the current EMMA session."""
        server = self._server
        self._server = None
        if server is not None:
            server.close()
        # Close the EMMA session before awaiting the listener: since Python
        # 3.12.1 Server.wait_closed() waits for every open connection, so
        # awaiting it first would block unloading while EMMA is connected.
        session = self.state.active_session
        if session is not None and not session.closed:
            session.writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(session.writer.wait_closed(), timeout=5)
        current = asyncio.current_task()
        handlers = [task for task in self._handlers if task is not current]
        if handlers:
            done, pending = await asyncio.wait(handlers, timeout=5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._handlers.clear()
        if server is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=5)
        log.info("Huawei reverse Modbus/TLS listener stopped")


class CommandApi:
    def __init__(self, state: RuntimeState, token: str):
        self.state = state
        self.token = token
        self._last_state_summary: tuple[bool, int, int] | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        method = "?"
        path = "?"
        try:
            header_bytes = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
            if len(header_bytes) > 16384:
                raise HttpError(431, "Request headers are too large")
            lines = header_bytes.decode("iso-8859-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, separator, value = line.partition(":")
                if not separator:
                    raise HttpError(400, "Malformed HTTP header")
                headers[name.strip().lower()] = value.strip()

            authorization = headers.get("authorization", "")
            expected = f"Bearer {self.token}"
            if not hmac.compare_digest(authorization, expected):
                log.warning(
                    "API authentication failed peer=%s method=%s target=%s",
                    peer,
                    method,
                    target,
                )
                raise HttpError(401, "Unauthorized")

            content_length = int(headers.get("content-length", "0"))
            if not 0 <= content_length <= MAX_HTTP_REQUEST_BODY:
                raise HttpError(413, "Request body is too large")
            body = await reader.readexactly(content_length) if content_length else b""
            path = urllib.parse.urlsplit(target).path
            if path in (API_DEVICE, API_ENTITIES):
                log.info("Authenticated API request peer=%s method=%s path=%s", peer, method, path)
            else:
                log.debug("Authenticated API request peer=%s method=%s path=%s", peer, method, path)

            if method == "GET" and path in ("/health", API_HEALTH):
                payload = self.state.health()
                if path == "/health":
                    payload = {
                        **payload,
                        "device_info": self.state.device().get("startup", {}),
                        "states": self.state.latest_states,
                    }
                await _send_json(writer, 200, payload)
            elif method == "GET" and path == API_DEVICE:
                await _send_json(writer, 200, self.state.device())
            elif method == "GET" and path == API_ENTITIES:
                await _send_json(
                    writer,
                    200,
                    {
                        "poll_intervals": POLL_INTERVALS,
                        "entities": self.state.entities(),
                    },
                )
            elif method == "GET" and path == API_STATES:
                session = self.state.active_session
                unsupported = sorted(session.unsupported_registers) if session else []
                summary = (
                    session is not None and not session.closed,
                    len(self.state.latest_values),
                    len(unsupported),
                )
                if summary != self._last_state_summary:
                    log.info(
                        "API state snapshot peer=%s connected=%s values=%d unsupported=%d last_success=%s",
                        peer,
                        summary[0],
                        summary[1],
                        summary[2],
                        session.last_poll_success if session else self.state.last_success,
                    )
                    self._last_state_summary = summary
                await _send_json(writer, 200, self.state.states())
            elif method == "GET" and path == API_DIAGNOSTICS:
                await _send_json(writer, 200, self.state.diagnostics())
            elif method == "POST" and path == API_SUBSCRIPTIONS:
                payload = _decode_json_object(body)
                try:
                    register_names = self.state.set_subscriptions(
                        payload.get("register_names")
                    )
                except ValueError as error:
                    raise HttpError(400, str(error)) from error
                log.info(
                    "API polling subscription accepted peer=%s registers=%d",
                    peer,
                    len(register_names),
                )
                await _send_json(
                    writer,
                    200,
                    {"ok": True, "register_names": register_names},
                )
            elif method == "POST" and path.startswith(f"{API_ENTITIES}/") and path.endswith("/value"):
                register_name = urllib.parse.unquote(
                    path.removeprefix(f"{API_ENTITIES}/").removesuffix("/value")
                ).strip("/")
                payload = _decode_json_object(body)
                if "value" not in payload:
                    raise HttpError(400, "JSON body must contain a value field")
                try:
                    value = await self.state.set_value(register_name, payload["value"])
                except ReadOnlyModeError as error:
                    raise HttpError(423, str(error)) from error
                except ValueError as error:
                    raise HttpError(400, str(error)) from error
                except (
                    TimeoutError,
                    ConnectionError,
                    ModbusProtocolError,
                    HuaweiSolarException,
                    WriteVerificationError,
                ) as error:
                    raise HttpError(502, str(error)) from error
                await _send_json(writer, 200, {"ok": True, "register_name": register_name, "value": value})
            elif method == "POST" and path == API_TOU_PERIODS:
                payload = _decode_json_object(body)
                try:
                    value = await self.state.set_tou_periods(payload.get("periods"))
                except ReadOnlyModeError as error:
                    raise HttpError(423, str(error)) from error
                except ValueError as error:
                    raise HttpError(400, str(error)) from error
                except (
                    TimeoutError,
                    ConnectionError,
                    ModbusProtocolError,
                    HuaweiSolarException,
                    WriteVerificationError,
                ) as error:
                    raise HttpError(502, str(error)) from error
                await _send_json(writer, 200, {"ok": True, "value": value})
            elif method == "POST" and path.startswith("/commands/"):
                command = path.removeprefix("/commands/")
                try:
                    payload = json.loads(body or b"{}")
                except json.JSONDecodeError as error:
                    raise HttpError(400, "Request body must be JSON") from error
                if not isinstance(payload, dict) or "value" not in payload:
                    raise HttpError(400, "JSON body must contain a value field")
                if self.state.read_only:
                    raise HttpError(423, READ_ONLY_MESSAGE)
                session = self.state.active_session
                if session is None or session.closed:
                    raise HttpError(503, "EMMA is not connected")
                try:
                    await session.execute_command(command, payload["value"])
                except ReadOnlyModeError as error:
                    raise HttpError(423, str(error)) from error
                except ValueError as error:
                    raise HttpError(400, str(error)) from error
                except (
                    TimeoutError,
                    ConnectionError,
                    ModbusProtocolError,
                    HuaweiSolarException,
                ) as error:
                    raise HttpError(502, str(error)) from error
                await _send_json(writer, 200, {"ok": True, "command": command})
            else:
                raise HttpError(404, "Not found")
        except HttpError as error:
            if error.status != 401:
                log.warning(
                    "API request failed peer=%s method=%s path=%s status=%d error=%s",
                    peer,
                    method,
                    path,
                    error.status,
                    error,
                )
            await _send_json(writer, error.status, {"error": str(error)})
        except (asyncio.IncompleteReadError, TimeoutError, ValueError):
            log.warning("Malformed API request from peer=%s", peer)
            await _send_json(writer, 400, {"error": "Malformed HTTP request"})
        except Exception:
            log.exception("Unexpected command API error")
            await _send_json(writer, 500, {"error": "Internal server error"})
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _connected_session(self) -> ReverseModbusSession:
        session = self.state.active_session
        if session is None or session.closed:
            raise HttpError(503, "EMMA is not connected")
        return session


def _decode_json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as error:
        raise HttpError(400, "Request body must be JSON") from error
    if not isinstance(payload, dict):
        raise HttpError(400, "JSON body must be an object")
    return payload


class HttpError(RuntimeError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


HTTP_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    413: "Content Too Large",
    423: "Locked",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


async def _send_json(writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
    body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
    reason = HTTP_REASONS.get(status, "Unknown")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(headers + body)
    await writer.drain()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Huawei EMMA reverse-connection Modbus/TLS management system"
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="TLS bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TLS bind port")
    parser.add_argument("--certfile", type=Path, default=DEFAULT_CERTFILE)
    parser.add_argument("--keyfile", type=Path, default=DEFAULT_KEYFILE)
    parser.add_argument("--ca-certfile", type=Path, default=DEFAULT_CA_CERTFILE)
    parser.add_argument("--ca-keyfile", type=Path, default=DEFAULT_CA_KEYFILE)
    parser.add_argument(
        "--cert-name",
        default=os.environ.get("EMMA_SERVER_NAME"),
        help="DNS name or IP used by EMMA for certificate validation",
    )
    parser.add_argument("--cafile", type=Path, default=None)
    parser.add_argument("--password", default=None, help="Encrypted private-key password")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Compatibility alias overriding the fast poll interval",
    )
    parser.add_argument("--fast-interval", type=float, default=30.0)
    parser.add_argument("--medium-interval", type=float, default=300.0)
    parser.add_argument("--slow-interval", type=float, default=1800.0)
    parser.add_argument("--request-timeout", type=float, default=5.0)
    parser.add_argument("--log-raw", action="store_true", help="Log complete Modbus frames")
    parser.add_argument("--once", action="store_true", help="Poll once, then disconnect")
    parser.add_argument(
        "--read-only",
        action="store_true",
        default=os.environ.get("EMMA_READ_ONLY", "").lower() in {"1", "true", "yes", "on"},
        help=(
            "EMMA read-only mode: keep telemetry and discovery but never send a "
            "Modbus write (for EMMA controlled by an external reserve provider). "
            "Also set by EMMA_READ_ONLY=true"
        ),
    )
    parser.add_argument(
        "--ha-url",
        default=os.environ.get("HA_URL"),
        help="Home Assistant base URL (or set HA_URL)",
    )
    parser.add_argument(
        "--legacy-ha-rest",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("ENABLE_LEGACY_HA_REST", "").lower()
        in ("1", "true", "yes", "on"),
        help="Opt in to legacy /api/states publishing (native integration recommended)",
    )
    parser.add_argument(
        "--ha-token-env",
        default="HA_TOKEN",
        help="Environment variable containing the Home Assistant token",
    )
    parser.add_argument(
        "--ha-ca-file",
        type=Path,
        default=Path(os.environ["HA_CA_FILE"]) if os.environ.get("HA_CA_FILE") else None,
        help="CA certificate used to verify Home Assistant HTTPS",
    )
    parser.add_argument("--api-host", default="0.0.0.0", help="Command API bind address")
    parser.add_argument("--api-port", type=int, default=DEFAULT_API_PORT, help="Command API port")
    parser.add_argument(
        "--api-token-env",
        default="EMMA_API_TOKEN",
        help="Environment variable containing the command API bearer token",
    )
    args = parser.parse_args()

    if args.poll_interval is not None:
        args.fast_interval = args.poll_interval
    if any(
        interval <= 0
        for interval in (args.fast_interval, args.medium_interval, args.slow_interval)
    ):
        parser.error("poll intervals must be greater than zero")
    if args.request_timeout <= 0:
        parser.error("--request-timeout must be greater than zero")
    if args.legacy_ha_rest and not args.ha_url:
        parser.error("--legacy-ha-rest requires --ha-url or HA_URL")
    if args.legacy_ha_rest and not os.environ.get(args.ha_token_env):
        parser.error(
            f"--legacy-ha-rest requires a token in environment variable {args.ha_token_env}"
        )
    return args


def build_ssl_context(args: argparse.Namespace) -> ssl.SSLContext:
    if not args.certfile.exists():
        raise FileNotFoundError(f"TLS certificate file not found: {args.certfile}")
    if not args.keyfile.exists():
        raise FileNotFoundError(f"TLS private key file not found: {args.keyfile}")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(
        certfile=str(args.certfile),
        keyfile=str(args.keyfile),
        password=args.password,
    )
    if args.cafile is not None:
        if not args.cafile.exists():
            raise FileNotFoundError(f"TLS CA file not found: {args.cafile}")
        context.load_verify_locations(cafile=str(args.cafile))
        context.verify_mode = ssl.CERT_REQUIRED
    return context


async def async_main(args: argparse.Namespace) -> None:
    status = ensure_certificates(
        args.certfile,
        args.keyfile,
        args.ca_certfile,
        args.ca_keyfile,
        args.cert_name,
        args.password,
    )
    if status.created_ca:
        log.warning(
            "Created local TLS CA at %s (SHA-256 %s). Import this CA in EMMA's "
            "third-party management system trust settings.",
            args.ca_certfile,
            status.fingerprint,
        )
    if status.created_server:
        log.info("Created/renewed TLS server certificate at %s", args.certfile)
    days_remaining = certificate_days_remaining(args.certfile)
    log.info("TLS certificate validation passed; %d days remaining", days_remaining)
    if days_remaining < 30:
        log.warning("TLS server certificate expires in %d days", days_remaining)

    ssl_context = build_ssl_context(args)
    publisher = None
    if args.legacy_ha_rest:
        publisher = HomeAssistantPublisher(
            args.ha_url,
            os.environ[args.ha_token_env],
            args.request_timeout,
            args.ha_ca_file,
        )

    state = RuntimeState(read_only=args.read_only)

    async def handle_emma(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if state.active_session is not None and not state.active_session.closed:
            log.warning("Rejecting additional EMMA connection from %s", peer)
            writer.close()
            await writer.wait_closed()
            return

        session = ReverseModbusSession(
            reader,
            writer,
            request_timeout=args.request_timeout,
            poll_interval=args.fast_interval,
            poll_intervals={
                "fast": args.fast_interval,
                "medium": args.medium_interval,
                "slow": args.slow_interval,
            },
            publisher=publisher,
            state_callback=state.update_states,
            subscribed_registers=state.subscribed_registers,
            log_raw=args.log_raw,
            once=args.once,
            read_only=state.read_only,
        )
        state.active_session = session
        state.connected_at = _now_iso()
        state.reconnect_count += 1
        log.info("TLS client connected from %s; management system is now Modbus master", peer)
        try:
            await session.run()
        finally:
            if state.active_session is session:
                state.active_session = None
            log.info("TLS client disconnected from %s", peer)

    tls_server = await asyncio.start_server(
        handle_emma,
        args.host,
        args.port,
        ssl=ssl_context,
    )
    addresses = ", ".join(str(socket.getsockname()) for socket in tls_server.sockets or [])
    log.info(
        "Huawei reverse Modbus/TLS listener ready on %s using cert=%s key=%s",
        addresses,
        args.certfile,
        args.keyfile,
    )

    api_server = None
    if args.api_port is not None:
        api_token = os.environ[args.api_token_env]
        token_fingerprint = hashlib.sha256(api_token.encode("utf-8")).hexdigest()[:12]
        api = CommandApi(state, api_token)
        api_server = await asyncio.start_server(api.handle, args.api_host, args.api_port)
        log.info(
            "Authenticated command API ready on %s:%d token_sha256=%s",
            args.api_host,
            args.api_port,
            token_fingerprint,
        )

    async with tls_server:
        if api_server is None:
            await tls_server.serve_forever()
        else:
            async with api_server:
                await asyncio.gather(
                    tls_server.serve_forever(),
                    api_server.serve_forever(),
                )


def main() -> None:
    from dotenv import load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    load_dotenv(override=True)
    args = parse_args()
    if args.api_token_env == "EMMA_API_TOKEN":
        token, created = load_or_create_environment()
    else:
        token = os.environ.get(args.api_token_env, "")
        created = False
        if not token:
            raise SystemExit(
                f"Missing API token in environment variable {args.api_token_env}"
            )
    if created:
        log.warning(
            "Generated EMMA_API_TOKEN and saved it to .env. Read it locally to "
            "configure Home Assistant; the token is never written to logs."
        )
    del token
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        log.info("Server graceful shutdown")


if __name__ == "__main__":
    main()
