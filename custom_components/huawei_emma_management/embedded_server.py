#!/usr/bin/env python3
"""Huawei EMMA reverse Modbus/TLS server shared by HA and standalone mode."""

import argparse
import asyncio
import contextlib
import hashlib
import hmac
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
from huawei_solar.exceptions import HuaweiSolarException, ReadException
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
        DEFAULT_API_PORT,
        DEFAULT_TLS_PORT,
        MAX_HTTP_REQUEST_BODY,
        MAX_MBAP_LENGTH,
        MAX_READ_REGISTERS,
        MAX_WRITE_REGISTERS,
    )
    from .embedded_catalog import (
        POLL_INTERVALS,
        EntityDescription,
        build_entity_catalog,
        grouped_register_names,
        json_value,
    )
    from .embedded_runtime_setup import (
        CertificateStatus,
        certificate_days_remaining,
        ensure_certificates,
        load_or_create_environment,
    )
except ImportError:  # Standalone external-server launcher.
    from connector_contract import (
        API_PREFIX,
        DEFAULT_API_PORT,
        DEFAULT_TLS_PORT,
        MAX_HTTP_REQUEST_BODY,
        MAX_MBAP_LENGTH,
        MAX_READ_REGISTERS,
        MAX_WRITE_REGISTERS,
    )
    from embedded_catalog import (
        POLL_INTERVALS,
        EntityDescription,
        build_entity_catalog,
        grouped_register_names,
        json_value,
    )
    from embedded_runtime_setup import (
        CertificateStatus,
        certificate_days_remaining,
        ensure_certificates,
        load_or_create_environment,
    )

log = logging.getLogger(__name__)

API_HEALTH = f"{API_PREFIX}/health"
API_DEVICE = f"{API_PREFIX}/device"
API_ENTITIES = f"{API_PREFIX}/entities"
API_STATES = f"{API_PREFIX}/states"
API_SUBSCRIPTIONS = f"{API_PREFIX}/subscriptions"
API_TOU_PERIODS = f"{API_PREFIX}/tou-periods"

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
    text = f"{model} {product_type or ''}".lower()
    if "emma" in text or "hems" in text:
        return "emma"
    if "smartguard" in text or "backupbox" in text:
        return "smartguard"
    if "sun2000" in text or "inverter" in text:
        return "inverter"
    if "smartlogger" in text:
        return "smartlogger"
    if "sdongle" in text or "smart dongle" in text:
        return "sdongle"
    if any(word in text for word in ("meter", "dtsu", "ddsu", "yds", "energy sensor")):
        return "external_meter"
    if "charger" in text or "wallbox" in text:
        return "charger"
    return "accessory"


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


def build_catalog_states(
    results: dict[str, Result[Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Create REST-compatible sensor states and API-native register values."""
    states: dict[str, dict[str, Any]] = {}
    values: dict[str, Any] = {}
    for register_name, decoded in results.items():
        description = ENTITY_CATALOG.get(str(register_name))
        if description is None:
            continue
        value = json_value(decoded.value)
        values[str(register_name)] = value
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
            state_value = len(value) if isinstance(value, list) else None
        elif description.device_class == "timestamp" and value is not None:
            state_value = datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
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
            function_code = error.function_code & 0x7F
            error_class = error_code_to_exception_map.get(
                error.exception_code,
                UnknownModbusResponseError,
            )
            raise error_class(error.exception_code, function_code) from error
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
    ):
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
        self.subscribed_registers = set(
            subscribed_registers
            if subscribed_registers is not None
            else {
                name
                for name, description in ENTITY_CATALOG.items()
                if description.enabled_default
            }
        )
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

    async def set_register_value(self, register_name: str, value: Any) -> Any:
        description = ENTITY_CATALOG.get(register_name)
        if description is None or not description.writeable:
            raise ValueError(f"Register is unknown or read-only: {register_name}")
        definition = REGISTERS[register_name]
        converted = _convert_register_value(description, definition.unit, value)
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
        if device is not None:
            await device.set(register_name, converted)
            decoded = await device.get(register_name)
        else:
            await client.set(register_name, converted)
            decoded = await client.get(register_name)
        normalized = json_value(decoded.value)
        self.latest_values[register_name] = normalized
        self.latest_updated_at[register_name] = _now_iso()
        return normalized

    async def set_tou_periods(self, periods: Any) -> Any:
        return await self.set_register_value(str(rn.EMMA_TOU_PERIODS), periods)

    async def execute_command(self, name: str, value: Any) -> None:
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
            self.devices_by_role["emma"] = self.device
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

        for topology_device in self.topology_devices:
            role = topology_device["role"]
            unit_id = topology_device.get("unit_id")
            if role not in (
                "inverter",
                "charger",
                "sdongle",
                "smartlogger",
            ) or not isinstance(unit_id, int):
                continue
            client = AsyncHuaweiSolarClient(self.transport, unit_id=unit_id)
            self.clients_by_role[role] = client
            try:
                device = await create_device_instance(client)
                self.devices_by_role[role] = device
                log.info(
                    "Initialized %s subdevice unit=%d model=%s serial=%s",
                    role,
                    unit_id,
                    device.model_name,
                    getattr(device, "serial_number", topology_device.get("serial_number") or "-"),
                )
            except (TimeoutError, ConnectionError, HuaweiSolarException) as error:
                log.warning(
                    "Could not initialize %s subdevice unit=%d; individual register access will be used: %s",
                    role,
                    unit_id,
                    error,
                )

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
                    and self._register_available_by_topology(register_name)
                ]
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
                    states, values = build_catalog_states(decoded)
                    timestamp = _now_iso()
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
                    log.warning("EMMA poll group=%s failed: %s", group, error)

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
        if not isinstance(value, list) or not 0 <= len(value) <= 14:
            raise ValueError("TOU periods must be a JSON list containing 0 to 14 periods")
        result: list[HUAWEI_LUNA2000_TimeOfUsePeriod] = []
        for index, period in enumerate(value):
            if not isinstance(period, dict):
                raise ValueError(f"TOU period {index + 1} must be an object")
            start = _minutes(period.get("start_time"), f"period {index + 1} start_time")
            end = _minutes(period.get("end_time"), f"period {index + 1} end_time")
            if not 0 <= start < end <= 1440:
                raise ValueError(
                    f"TOU period {index + 1} must have 0 <= start < end <= 1440"
                )
            action = period.get("action", period.get("charge_flag"))
            try:
                charge_flag = (
                    ChargeFlag[action.upper()]
                    if isinstance(action, str)
                    else ChargeFlag(int(action))
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"TOU period {index + 1} action must be charge or discharge"
                ) from error
            days = period.get("days", period.get("days_effective"))
            if (
                not isinstance(days, (list, tuple))
                or len(days) != 7
                or any(not isinstance(day, bool) for day in days)
            ):
                raise ValueError(
                    f"TOU period {index + 1} days must contain seven booleans (Mon-Sun)"
                )
            result.append(
                HUAWEI_LUNA2000_TimeOfUsePeriod(
                    start_time=start,
                    end_time=end,
                    charge_flag=charge_flag,
                    days_effective=tuple(days),
                )
            )
        return result

    if isinstance(unit, type) and issubclass(unit, IntEnum):
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


def _minutes(value: Any, field_name: str) -> int:
    if isinstance(value, str) and ":" in value:
        hour_text, minute_text = value.split(":", 1)
        try:
            hour, minute = int(hour_text), int(minute_text)
        except ValueError as error:
            raise ValueError(f"{field_name} must be HH:MM or minutes") from error
        if not 0 <= hour <= 24 or not 0 <= minute <= 59 or (hour == 24 and minute):
            raise ValueError(f"{field_name} is not a valid time")
        return hour * 60 + minute
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be HH:MM or minutes") from error


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
        default_factory=lambda: {
            name
            for name, description in ENTITY_CATALOG.items()
            if description.enabled_default
        }
    )

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
        subscriptions = set(register_names)
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
        }

    def connected_session(self) -> ReverseModbusSession:
        """Return the active EMMA session or fail clearly."""
        session = self.active_session
        if session is None or session.closed:
            raise ConnectionError("EMMA is not connected")
        return session

    async def set_value(self, register_name: str, value: Any) -> Any:
        """Validate, write and cache a register value."""
        session = self.connected_session()
        result = await session.set_register_value(register_name, value)
        self.latest_values[register_name] = result
        self.updated_at[register_name] = session.latest_updated_at[register_name]
        return result

    async def set_tou_periods(self, periods: Any) -> Any:
        """Validate, write and cache the full EMMA TOU schedule."""
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


class ReverseModbusTlsServer:
    """Lifecycle-managed server suitable for a Home Assistant config entry."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.state = RuntimeState()
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
            await server.wait_closed()
        session = self.state.active_session
        if session is not None and not session.closed:
            session.writer.close()
            with contextlib.suppress(Exception):
                await session.writer.wait_closed()
        current = asyncio.current_task()
        handlers = [task for task in self._handlers if task is not current]
        if handlers:
            done, pending = await asyncio.wait(handlers, timeout=5)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        self._handlers.clear()
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
                await _send_json(
                    writer,
                    200,
                    {
                        "values": self.state.latest_values,
                        "updated_at": self.state.updated_at,
                        "unsupported": unsupported,
                    },
                )
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
                except ValueError as error:
                    raise HttpError(400, str(error)) from error
                except (TimeoutError, ConnectionError, ModbusProtocolError, HuaweiSolarException) as error:
                    raise HttpError(502, str(error)) from error
                await _send_json(writer, 200, {"ok": True, "register_name": register_name, "value": value})
            elif method == "POST" and path == API_TOU_PERIODS:
                payload = _decode_json_object(body)
                try:
                    value = await self.state.set_tou_periods(payload.get("periods"))
                except ValueError as error:
                    raise HttpError(400, str(error)) from error
                except (TimeoutError, ConnectionError, ModbusProtocolError, HuaweiSolarException) as error:
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
                session = self.state.active_session
                if session is None or session.closed:
                    raise HttpError(503, "EMMA is not connected")
                try:
                    await session.execute_command(command, payload["value"])
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

    state = RuntimeState()

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
