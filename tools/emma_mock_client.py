#!/usr/bin/env python3
"""Networked Huawei EMMA mock: TLS client and Modbus/TCP slave."""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import ssl
import struct
from dataclasses import dataclass
from pathlib import Path


LOG = logging.getLogger("emma-mock")
MAX_MBAP_LENGTH = 512
CORE_ADDRESS = 30354
CORE_COUNT = 20
TOU_ADDRESS = 40004
TOU_COUNT = 43

EMMA_INFO = (
    "1=EMMA-A02;2=V100R025C00SPC115;3=P1.15-D1.0;"
    "4=MOCK-EMMA-0001;5=0;6=1.0;8=HEMS;9=0"
)
SMARTGUARD_INFO = (
    "1=SmartGuard-63A-T0;2=V100R024C00SPC104;3=P1.15-D5.0;"
    "4=MOCK-GUARD-0001;5=2;6=1.1;8=BackupBox;9=0"
)
INVERTER_INFO = (
    "1=SUN2000-10KTL-M1;2=V100R001C00SPC174;3=P1.15-D5.0;"
    "4=MOCK-INVERTER-0001;5=3;6=1.1;9=0"
)


@dataclass(slots=True)
class ModbusFrame:
    transaction: int
    protocol: int
    unit: int
    pdu: bytes


def _u32(value: int) -> tuple[int, int]:
    value &= 0xFFFFFFFF
    return value >> 16, value & 0xFFFF


def _exception(function: int, code: int) -> bytes:
    return bytes((function | 0x80, code))


def build_startup_pdu() -> bytes:
    """Build the private Huawei 0x41 startup PDU captured from a real EMMA."""
    prefix = bytes.fromhex("41 44 01 0A 01 00 00 00 00 16 00 01 01 00 12 FE")
    content = prefix + EMMA_INFO.encode("ascii")
    if len(content) > 270:
        raise ValueError("Mock startup identity exceeds Huawei's 270-byte PDU")
    return content.ljust(270, b"\x00")


def encode_tou_periods(periods: list[dict[str, int]]) -> list[int]:
    registers = [0] * TOU_COUNT
    registers[0] = len(periods)
    for index, period in enumerate(periods):
        registers[1 + index * 3] = period["start"]
        registers[2 + index * 3] = period["end"]
        registers[3 + index * 3] = (
            (period["charge_flag"] & 0xFF) << 8
        ) | (period["days"] & 0x7F)
    return registers


def decode_tou_periods(registers: list[int]) -> list[dict[str, int]]:
    if len(registers) != TOU_COUNT or registers[0] > 14:
        raise ValueError("TOU register block has an invalid period count")
    periods: list[dict[str, int]] = []
    for index in range(registers[0]):
        period = {
            "start": registers[1 + index * 3],
            "end": registers[2 + index * 3],
            "charge_flag": registers[3 + index * 3] >> 8,
            "days": registers[3 + index * 3] & 0x7F,
        }
        if not 0 <= period["start"] < period["end"] <= 1440:
            raise ValueError("TOU period has an invalid time range")
        if period["charge_flag"] not in (0, 1) or period["days"] == 0:
            raise ValueError("TOU period has an invalid mode or weekday mask")
        periods.append(period)
    for day in range(7):
        enabled = [period for period in periods if period["days"] & (1 << day)]
        enabled.sort(key=lambda period: period["start"])
        for left, right in zip(enabled, enabled[1:]):
            if left["end"] > right["start"]:
                raise ValueError("TOU periods overlap on an enabled weekday")
    return periods


class EmmaMockDevice:
    """Stateful Modbus slave implementing the subset used by the ESP32."""

    def __init__(self, pv_power: int = 3400, load_power: int = 2364) -> None:
        self.base_pv_power = pv_power
        self.base_load_power = load_power
        self.core_read_count = 0
        self.request_count = 0
        self.tou_registers = encode_tou_periods(
            [
                {"start": 0, "end": 270, "charge_flag": 0, "days": 0x7F},
                {"start": 420, "end": 1439, "charge_flag": 1, "days": 0x7F},
            ]
        )

    def _core_registers(self) -> dict[int, int]:
        phase = self.core_read_count * 0.35
        pv = max(0, round(self.base_pv_power + 320 * math.sin(phase)))
        load = max(0, round(self.base_load_power + 180 * math.cos(phase * 0.7)))
        feed_in = pv - load
        battery = round(250 * math.sin(phase * 0.5))
        values = [0] * CORE_COUNT

        def put32(offset: int, value: int) -> None:
            values[offset], values[offset + 1] = _u32(value)

        put32(0, pv)
        put32(2, load)
        put32(4, feed_in)
        put32(6, battery)
        put32(8, 10_000)
        put32(10, pv - battery)
        values[14] = 5650  # 56.50%
        put32(15, 20_700)  # 20.700 kWh
        put32(17, 18_000)  # 18.000 kWh
        values[19] = 2000  # 20.00%
        return {CORE_ADDRESS + index: value for index, value in enumerate(values)}

    @staticmethod
    def _device_page(object_id: int) -> bytes:
        pages = {
            0x87: (0xFF, 0x88, [(0x87, b"\x03")]),
            0x88: (0xFF, 0x89, [(0x88, EMMA_INFO.encode("ascii"))]),
            0x89: (0xFF, 0x8A, [(0x89, SMARTGUARD_INFO.encode("ascii"))]),
            0x8A: (0x00, 0x00, [(0x8A, INVERTER_INFO.encode("ascii"))]),
        }
        more, next_object, objects = pages.get(object_id, (0x00, 0x00, []))
        response = bytearray((0x2B, 0x0E, 0x03, 0x03, more, next_object, len(objects)))
        for current_id, value in objects:
            response.extend((current_id, len(value)))
            response.extend(value)
        return bytes(response)

    def handle_pdu(self, pdu: bytes) -> bytes:
        self.request_count += 1
        if not pdu:
            return _exception(0, 0x03)
        function = pdu[0]
        if function == 0x2B:
            if len(pdu) != 4 or pdu[1:3] != b"\x0e\x03":
                return _exception(function, 0x03)
            return self._device_page(pdu[3])
        if function == 0x03:
            if len(pdu) != 5:
                return _exception(function, 0x03)
            address, count = struct.unpack(">HH", pdu[1:5])
            if count < 1 or count > 125:
                return _exception(function, 0x03)
            if address == CORE_ADDRESS and count == CORE_COUNT:
                self.core_read_count += 1
            registers = self._core_registers()
            registers.update(
                {TOU_ADDRESS + index: value for index, value in enumerate(self.tou_registers)}
            )
            if any(address + offset not in registers for offset in range(count)):
                return _exception(function, 0x02)
            data = b"".join(
                struct.pack(">H", registers[address + offset]) for offset in range(count)
            )
            return bytes((function, len(data))) + data
        if function == 0x10:
            if len(pdu) < 6:
                return _exception(function, 0x03)
            address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
            if len(pdu) != 6 + byte_count or byte_count != count * 2:
                return _exception(function, 0x03)
            if address != TOU_ADDRESS or count != TOU_COUNT:
                return _exception(function, 0x02)
            values = list(struct.unpack(f">{count}H", pdu[6:]))
            try:
                periods = decode_tou_periods(values)
            except ValueError as error:
                LOG.warning("Rejected TOU write: %s", error)
                return _exception(function, 0x03)
            self.tou_registers = values
            LOG.info("Accepted TOU write with %d periods", len(periods))
            return bytes((function,)) + struct.pack(">HH", address, count)
        return _exception(function, 0x01)


async def read_frame(reader: asyncio.StreamReader) -> ModbusFrame:
    header = await reader.readexactly(6)
    transaction, protocol, length = struct.unpack(">HHH", header)
    if length < 2 or length > MAX_MBAP_LENGTH:
        raise ValueError(f"Invalid MBAP length {length}")
    body = await reader.readexactly(length)
    return ModbusFrame(transaction, protocol, body[0], body[1:])


async def write_frame(
    writer: asyncio.StreamWriter,
    transaction: int,
    unit: int,
    pdu: bytes,
) -> bytes:
    frame = struct.pack(">HHHB", transaction, 0, len(pdu) + 1, unit) + pdu
    writer.write(frame)
    await writer.drain()
    return frame


def describe_pdu(pdu: bytes) -> str:
    if not pdu:
        return "empty"
    function = pdu[0]
    if function in (0x03, 0x10) and len(pdu) >= 5:
        address, count = struct.unpack(">HH", pdu[1:5])
        return f"fc=0x{function:02X} address={address} (0x{address:04X}) count={count}"
    if function == 0x2B and len(pdu) >= 4:
        return f"fc=0x2B object=0x{pdu[3]:02X}"
    if function & 0x80 and len(pdu) >= 2:
        return f"fc=0x{function:02X} exception={pdu[1]}"
    return f"fc=0x{function:02X} bytes={len(pdu)}"


def describe_response_pdu(pdu: bytes) -> str:
    if not pdu:
        return "empty"
    function = pdu[0]
    if function & 0x80 and len(pdu) >= 2:
        return f"fc=0x{function:02X} exception={pdu[1]}"
    if function == 0x03 and len(pdu) >= 2:
        return f"fc=0x03 byte_count={pdu[1]}"
    if function == 0x10 and len(pdu) == 5:
        address, count = struct.unpack(">HH", pdu[1:5])
        return f"fc=0x10 address={address} (0x{address:04X}) count={count}"
    if function == 0x2B and len(pdu) >= 7:
        return (
            f"fc=0x2B more={pdu[4] != 0} next=0x{pdu[5]:02X} "
            f"objects={pdu[6]}"
        )
    return f"fc=0x{function:02X} bytes={len(pdu)}"


def tls_context(ca_file: Path | None, insecure: bool) -> ssl.SSLContext:
    if insecure:
        context = ssl._create_unverified_context()  # noqa: SLF001 - explicit lab option
        LOG.warning("TLS certificate verification is disabled")
    else:
        if ca_file is None:
            raise ValueError("Use --ca-file with the ESP32 CA, or explicitly use --insecure")
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


async def run_connection(args: argparse.Namespace, device: EmmaMockDevice) -> None:
    context = tls_context(args.ca_file, args.insecure)
    local_address = (args.local_address, 0) if args.local_address else None
    server_hostname = None if args.insecure else (args.server_name or args.host)
    LOG.info("Connecting mock EMMA to %s:%d", args.host, args.port)
    reader, writer = await asyncio.open_connection(
        args.host,
        args.port,
        ssl=context,
        server_hostname=server_hostname,
        local_addr=local_address,
    )
    peer = writer.get_extra_info("peername")
    cipher = writer.get_extra_info("cipher")
    LOG.info("TLS connected peer=%s cipher=%s", peer, cipher[0] if cipher else "unknown")
    startup = await write_frame(writer, 0, 0, build_startup_pdu())
    LOG.info("TX mock -> ESP32 Huawei startup model=EMMA-A02 serial=MOCK-EMMA-0001")
    if args.log_raw:
        LOG.debug("TX raw=%s", startup.hex(" ").upper())
    handled = 0
    try:
        while True:
            frame = await read_frame(reader)
            handled += 1
            LOG.info(
                "RX ESP32 -> mock transaction=%d unit=%d %s",
                frame.transaction,
                frame.unit,
                describe_pdu(frame.pdu),
            )
            if args.log_raw:
                LOG.debug("RX PDU raw=%s", frame.pdu.hex(" ").upper())
            if args.delay_ms:
                await asyncio.sleep(args.delay_ms / 1000)
            response = device.handle_pdu(frame.pdu)
            raw = await write_frame(writer, frame.transaction, frame.unit, response)
            LOG.info(
                "TX mock -> ESP32 transaction=%d unit=%d %s",
                frame.transaction,
                frame.unit,
                describe_response_pdu(response),
            )
            if args.log_raw:
                LOG.debug("TX raw=%s", raw.hex(" ").upper())
            if args.disconnect_after and handled >= args.disconnect_after:
                LOG.warning("Intentional disconnect after %d requests", handled)
                return
    finally:
        writer.close()
        await writer.wait_closed()


async def run(args: argparse.Namespace) -> None:
    device = EmmaMockDevice(args.pv_power, args.load_power)
    while True:
        try:
            await run_connection(args, device)
        except (OSError, ssl.SSLError, asyncio.IncompleteReadError, ValueError) as error:
            LOG.warning("Mock connection ended: %s", error)
        if args.once:
            return
        LOG.info("Reconnecting in %.1f seconds", args.reconnect_delay)
        await asyncio.sleep(args.reconnect_delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.88.20", help="ESP32 W5500 address")
    parser.add_argument("--port", type=int, default=16100, help="ESP32 Modbus/TLS port")
    parser.add_argument("--ca-file", type=Path, help="CA certificate that signed the ESP32 server certificate")
    parser.add_argument("--insecure", action="store_true", help="disable certificate verification for isolated lab testing")
    parser.add_argument("--server-name", help="certificate DNS/IP name when it differs from --host")
    parser.add_argument("--local-address", help="Armbian source address on the EMMA test network")
    parser.add_argument("--pv-power", type=int, default=3400, help="base mock PV power in watts")
    parser.add_argument("--load-power", type=int, default=2364, help="base mock load power in watts")
    parser.add_argument("--delay-ms", type=int, default=0, help="delay each Modbus response")
    parser.add_argument("--disconnect-after", type=int, default=0, help="disconnect after this many requests")
    parser.add_argument("--reconnect-delay", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="do not reconnect after disconnection")
    parser.add_argument("--log-raw", action="store_true", help="log full MBAP/PDU frames at DEBUG")
    parser.add_argument("--debug", action="store_true", help="enable DEBUG logging")
    args = parser.parse_args()
    if args.insecure and args.ca_file:
        parser.error("--insecure and --ca-file are mutually exclusive")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.delay_ms < 0 or args.disconnect_after < 0:
        parser.error("delay and disconnect values cannot be negative")
    return args


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug or args.log_raw else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        LOG.info("Mock stopped")


if __name__ == "__main__":
    main()
