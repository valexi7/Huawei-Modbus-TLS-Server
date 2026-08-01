import ast
import asyncio
import json
import os
import struct
import subprocess
import tempfile
import unittest
import ipaddress
import importlib.util
import sys
import types
from types import SimpleNamespace
from pathlib import Path

from cryptography import x509

from huawei_solar import register_names as rn
from huawei_solar.register_definitions import Result
from huawei_solar.exceptions import ReadException

from huawei_modbus_server import (
    CommandApi,
    CORE_REGISTER_NAMES,
    ENTITY_CATALOG,
    ModbusFrame,
    ReverseModbusSession,
    RuntimeState,
    ReverseModbusTlsServer,
    ServerConfig,
    _convert_register_value,
    build_catalog_states,
    build_core_sensor_states,
    describe_frame,
    parse_device_identification_page,
    parse_huawei_device_info,
    parse_topology_devices,
    read_modbus_frame,
)
from emma_catalog import build_entity_catalog, register_client_role
from huawei_solar.registers import REGISTERS
from runtime_setup import ensure_certificates, load_or_create_environment
from tools.emma_mock_client import (
    CORE_ADDRESS as MOCK_CORE_ADDRESS,
    CORE_COUNT as MOCK_CORE_COUNT,
    TOU_ADDRESS as MOCK_TOU_ADDRESS,
    TOU_COUNT as MOCK_TOU_COUNT,
    EmmaMockDevice,
    build_startup_pdu,
    decode_tou_periods,
    encode_tou_periods,
)


CORE_REGISTER_COUNT = 20


class RepositoryContractTests(unittest.TestCase):
    def test_esphome_reverse_connector_contract(self):
        component = Path("esphome/components/huawei_emma_reverse")
        source = (component / "huawei_emma_reverse.cpp").read_text(encoding="utf-8")
        generated = (component / "generated_register_catalog.h").read_text(
            encoding="utf-8"
        )
        schema = (component / "__init__.py").read_text(encoding="utf-8")
        device_yaml = Path("esphome/huawei-emma-tls-server.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("mbedtls_net_bind", source)
        self.assertIn(
            "generated_register_catalog.h",
            (component / "huawei_emma_reverse.h").read_text(encoding="utf-8"),
        )
        self.assertIn("GENERATED_ENTITY_COUNT = 740", generated)
        self.assertIn("GENERATED_CONTRACT_VERSION = 2", generated)
        self.assertIn('"emma_tou_periods"', generated)
        self.assertIn("GENERATED_CATALOG_SHA256", source)
        self.assertIn("GENERATED_PATH_TOU", source)
        self.assertIn('GENERATED_PATH_TOU[] = "/api/v1/tou-periods"', generated)
        self.assertIn("ActivityKind::MODBUS_RX", source)
        self.assertIn("ActivityKind::API_TX", source)
        self.assertIn("Polling subscription added group=%s name=%s", source)
        self.assertIn("subscribed=%u requested=%u batches=%u", source)
        self.assertIn("CONF_CERTIFICATE", schema)
        self.assertIn("CONF_PRIVATE_KEY", schema)
        self.assertIn("huawei_emma_reverse:", device_yaml)

        migration = json.loads(
            Path("esphome/entity-migration-map.json").read_text(encoding="utf-8")
        )
        self.assertEqual(migration["physical_entity_count"], 740)
        self.assertEqual(migration["virtual_entity_count"], 10)
        self.assertEqual(migration["firmware_supported_count"], 740)
        check = subprocess.run(
            [sys.executable, "tools/generate_esphome_catalog.py", "--check"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_public_actions_are_consolidated_under_huawei_emma(self):
        source = Path(
            "custom_components/huawei_emma_management/__init__.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"structured_periods"', source)
        self.assertIn('"read_time_segments"', source)
        self.assertIn('"read_tou_periods"', source)
        self.assertIn('"update_time_segment"', source)
        self.assertIn("_set_huawei_service_schemas(hass)", source)
        self.assertIn('vol.Optional("device_id")', source)
        self.assertIn('device_field["example"] = parent_device_ids[0]', source)
        self.assertIn('"00:00-03:59/1234567/+\\n"', source)
        self.assertIn('"active_tou"', source)
        self.assertIn("_device_response_metadata", source)
        self.assertIn('"native_emma_connector_poll"', source)
        self.assertIn("EVENT_ENTITY_REGISTRY_UPDATED", source)
        self.assertIn("async_sync_polling_subscriptions", source)
        services = Path(
            "custom_components/huawei_emma_management/services.yaml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("set_tou_periods:", services)
        self.assertNotIn("read_time_segments:", services)
        self.assertNotIn("update_time_segment:", services)

    def test_config_flow_port_fields_use_serializable_ha_validator(self):
        source = Path(
            "custom_components/huawei_emma_management/config_flow.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)

        defined_functions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn("_port", defined_functions)

        cv_port_references = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "cv"
            and node.attr == "port"
        ]
        self.assertEqual(len(cv_port_references), 4)


class EmmaMockTests(unittest.TestCase):
    def test_startup_and_paged_topology_match_esp32_contract(self):
        startup = build_startup_pdu()
        self.assertEqual(len(startup), 270)
        self.assertEqual(startup[0], 0x41)
        self.assertIn(b"1=EMMA-A02", startup)

        device = EmmaMockDevice()
        first = device.handle_pdu(bytes((0x2B, 0x0E, 0x03, 0x87)))
        self.assertEqual(first[:7], bytes((0x2B, 0x0E, 0x03, 0x03, 0xFF, 0x88, 0x01)))
        emma = device.handle_pdu(bytes((0x2B, 0x0E, 0x03, 0x88)))
        self.assertIn(b"MOCK-EMMA-0001", emma)
        inverter = device.handle_pdu(bytes((0x2B, 0x0E, 0x03, 0x8A)))
        self.assertEqual(inverter[4:7], bytes((0x00, 0x00, 0x01)))

    def test_core_values_change_and_unknown_ranges_are_rejected(self):
        device = EmmaMockDevice()
        request = bytes((0x03,)) + struct.pack(">HH", MOCK_CORE_ADDRESS, MOCK_CORE_COUNT)
        first = device.handle_pdu(request)
        second = device.handle_pdu(request)
        self.assertEqual(first[:2], bytes((0x03, MOCK_CORE_COUNT * 2)))
        self.assertEqual(len(first), 2 + MOCK_CORE_COUNT * 2)
        self.assertNotEqual(first, second)
        unknown = device.handle_pdu(bytes((0x03,)) + struct.pack(">HH", 1234, 1))
        self.assertEqual(unknown, bytes((0x83, 0x02)))

    def test_tou_write_is_retained_for_readback(self):
        device = EmmaMockDevice()
        periods = [
            {"start": 0, "end": 360, "charge_flag": 0, "days": 0x7F},
            {"start": 1020, "end": 1200, "charge_flag": 1, "days": 0x1F},
        ]
        registers = encode_tou_periods(periods)
        payload = struct.pack(f">{MOCK_TOU_COUNT}H", *registers)
        write = (
            bytes((0x10,))
            + struct.pack(">HHB", MOCK_TOU_ADDRESS, MOCK_TOU_COUNT, len(payload))
            + payload
        )
        response = device.handle_pdu(write)
        self.assertEqual(
            response,
            bytes((0x10,)) + struct.pack(">HH", MOCK_TOU_ADDRESS, MOCK_TOU_COUNT),
        )
        read = device.handle_pdu(
            bytes((0x03,)) + struct.pack(">HH", MOCK_TOU_ADDRESS, MOCK_TOU_COUNT)
        )
        values = list(struct.unpack(f">{MOCK_TOU_COUNT}H", read[2:]))
        self.assertEqual(decode_tou_periods(values), periods)

class FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closing = False

    def get_extra_info(self, name):
        return ("192.0.2.10", 43097) if name == "peername" else None

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def is_closing(self):
        return self.closing

    def close(self):
        self.closing = True

    async def wait_closed(self):
        return None


async def ignore_states(_session, _states):
    return None


class FrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_managed_tls_server_generates_material_and_stops(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cert_dir = Path(temp_dir)
            server = ReverseModbusTlsServer(
                ServerConfig(
                    host="127.0.0.1",
                    port=0,
                    certfile=cert_dir / "server-cert.pem",
                    keyfile=cert_dir / "server-key.pem",
                    ca_certfile=cert_dir / "ca-cert.pem",
                    ca_keyfile=cert_dir / "ca-key.pem",
                    cert_name="127.0.0.1",
                )
            )
            await server.async_start()
            self.assertTrue(server.running)
            self.assertTrue((cert_dir / "ca-cert.pem").is_file())
            self.assertTrue((cert_dir / "server-cert.pem").is_file())
            self.assertIsNotNone(server.certificate_status)
            self.assertFalse(server.state.health()["connected"])
            await server.async_stop()
            self.assertFalse(server.running)

    async def test_versioned_api_requires_token_and_serves_catalog(self):
        state = RuntimeState()
        subscription_updates = []
        state.active_session = SimpleNamespace(
            topology_complete=True,
            available_device_roles={"emma", "smartguard", "inverter"},
            closed=False,
            set_subscriptions=lambda names: subscription_updates.append(names),
        )
        api = CommandApi(state, "test-token")
        server = await asyncio.start_server(api.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET /api/v1/entities HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Authorization: Bearer test-token\r\n\r\n"
            )
            await writer.drain()
            response = await reader.read()
            self.assertIn(b"HTTP/1.1 200 OK", response)
            self.assertIn(b'"register_name":"model_name"', response)
            self.assertIn(b'"register_name":"emma_external_meter_running_status"', response)
            self.assertIn(b'"enabled_default":false', response)
            writer.close()
            await writer.wait_closed()

            body = b'{"register_names":["pv_output_power"]}'
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"POST /api/v1/subscriptions HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Authorization: Bearer test-token\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            response = await reader.read()
            self.assertIn(b"HTTP/1.1 200 OK", response)
            self.assertEqual(state.subscribed_registers, {"pv_output_power"})
            self.assertEqual(subscription_updates, [{"pv_output_power"}])
            writer.close()
            await writer.wait_closed()

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /api/v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            response = await reader.read()
            self.assertIn(b"HTTP/1.1 401 Unauthorized", response)
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()
    async def test_round_trip_frame_with_huawei_extended_length(self):
        original = ModbusFrame(0, 0, 0, b"\x41" + bytes(269))
        reader = asyncio.StreamReader()
        reader.feed_data(original.encode())
        reader.feed_eof()
        decoded = await read_modbus_frame(reader)
        self.assertEqual(decoded, original)

    async def test_master_correlates_read_response(self):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        session = ReverseModbusSession(
            reader,
            writer,
            request_timeout=1,
            poll_interval=10,
            publisher=None,
            state_callback=ignore_states,
        )

        task = asyncio.create_task(session.read_holding_registers(0, 30354, 2))
        await asyncio.sleep(0)
        transaction_id = struct.unpack(">H", writer.data[:2])[0]
        session._dispatch(
            ModbusFrame(transaction_id, 0, 0, b"\x03\x04\x00\x01\xff\xfe")
        )
        self.assertEqual(await task, [1, 65534])

    async def test_command_write_uses_library_register_and_scaling(self):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        session = ReverseModbusSession(
            reader,
            writer,
            request_timeout=1,
            poll_interval=10,
            publisher=None,
            state_callback=ignore_states,
        )

        task = asyncio.create_task(
            session.execute_command("max_grid_feed_in_percent", 42.5)
        )
        await asyncio.sleep(0)
        transaction_id = struct.unpack(">H", writer.data[:2])[0]
        request_pdu = bytes(writer.data[7:])
        self.assertEqual(request_pdu, struct.pack(">BHH", 0x06, 40109, 425))
        session._dispatch(ModbusFrame(transaction_id, 0, 0, request_pdu))
        await task

    async def test_device_list_follows_emma_pagination(self):
        reader = asyncio.StreamReader()
        writer = FakeWriter()
        session = ReverseModbusSession(
            reader,
            writer,
            request_timeout=1,
            publisher=None,
            state_callback=ignore_states,
        )
        task = asyncio.create_task(session.read_device_list())
        await asyncio.sleep(0)
        first_id = struct.unpack(">H", writer.data[:2])[0]
        self.assertEqual(bytes(writer.data[7:]), b"\x2b\x0e\x03\x87")
        session._dispatch(
            ModbusFrame(first_id, 0, 0, b"\x2b\x0e\x03\x03\xff\x88\x04\x87\x01\x03")
        )
        await asyncio.sleep(0)
        second_offset = 11
        second_id = struct.unpack(">H", writer.data[second_offset : second_offset + 2])[0]
        self.assertEqual(bytes(writer.data[second_offset + 7 :]), b"\x2b\x0e\x03\x88")
        session._dispatch(
            ModbusFrame(second_id, 0, 0, b"\x2b\x0e\x03\x03\x00\x00\x04\x88\x03INV")
        )
        self.assertEqual(await task, {0x87: b"\x03", 0x88: b"INV"})

    async def test_absent_optional_device_registers_are_skipped(self):
        session = ReverseModbusSession(
            asyncio.StreamReader(),
            FakeWriter(),
            request_timeout=1,
            publisher=None,
            state_callback=ignore_states,
        )
        session.topology_complete = True
        session.available_device_roles = {"emma", "smartguard", "inverter"}
        self.assertFalse(
            session._register_available_by_topology(
                "emma_external_meter_running_status"
            )
        )
        self.assertTrue(session._register_available_by_topology("inverter_active_power"))

    async def test_illegal_data_value_is_isolated_as_unsupported(self):
        class RejectingDevice:
            async def batch_update(self, _names):
                raise ReadException("not installed", modbus_exception_code=3)

        session = ReverseModbusSession(
            asyncio.StreamReader(),
            FakeWriter(),
            request_timeout=1,
            publisher=None,
            state_callback=ignore_states,
        )
        session.device = RejectingDevice()
        session.devices_by_role["emma"] = session.device
        result = await session._read_registers_with_fallback(
            ["pv_output_power"]
        )
        self.assertEqual(result, {})
        self.assertIn("pv_output_power", session.unsupported_registers)

    async def test_tou_schedule_is_read_individually(self):
        class StructuredDevice:
            async def get(self, name):
                self.name = name
                return Result([], None)

            async def batch_update(self, _names):
                raise AssertionError("structured schedules must not use batch_update")

        session = ReverseModbusSession(
            asyncio.StreamReader(),
            FakeWriter(),
            request_timeout=1,
            publisher=None,
            state_callback=ignore_states,
        )
        device = StructuredDevice()
        session.devices_by_role["emma"] = device
        result = await session._read_registers_with_fallback([str(rn.EMMA_TOU_PERIODS)])
        self.assertEqual(result[str(rn.EMMA_TOU_PERIODS)].value, [])

    async def test_inverter_control_routes_to_subdevice_and_honors_rating(self):
        class InverterClient:
            async def set(self, name, value):
                self.written = (name, value)

            async def get(self, _name):
                return Result(5000, "W")

        session = ReverseModbusSession(
            asyncio.StreamReader(),
            FakeWriter(),
            request_timeout=1,
            publisher=None,
            state_callback=ignore_states,
        )
        client = InverterClient()
        session.clients_by_role["inverter"] = client
        session.latest_values[str(rn.INVERTER_RATED_POWER)] = 10000
        register_name = str(rn.STORAGE_MAXIMUM_CHARGING_POWER)
        self.assertEqual(await session.set_register_value(register_name, 5000), 5000)
        self.assertEqual(client.written, (register_name, 5000))
        with self.assertRaisesRegex(ValueError, "rated power"):
            await session.set_register_value(register_name, 10100)


class DecoderTests(unittest.TestCase):
    def test_friendly_names_do_not_expand_ac_inside_active(self):
        catalog = build_entity_catalog()
        self.assertEqual(
            catalog["active_power_built_in_energy"].name,
            "Built-in Meter Active Power",
        )
        self.assertEqual(
            catalog["phase_a_voltage_built_in_energy"].name,
            "Built-in Meter Phase A Voltage",
        )
        self.assertEqual(
            catalog["phase_c_current_external_energy"].name,
            "External Meter Phase C Current",
        )
        self.assertEqual(
            catalog["line_voltage_a_b_built_in_energy"].name,
            "Built-in Meter Line Voltage A-B",
        )
        self.assertEqual(
            catalog["total_positive_active_energy_built_in_energy"].name,
            "Built-in Meter Total Positive Active Energy",
        )

        self.assertEqual(catalog["phase_a_voltage_built_in_energy"].unit, "V")
        self.assertEqual(
            catalog["phase_a_voltage_built_in_energy"].device_class, "voltage"
        )
        self.assertEqual(catalog["phase_a_current_built_in_energy"].unit, "A")
        self.assertEqual(
            catalog["phase_a_current_built_in_energy"].device_class, "current"
        )
        self.assertEqual(catalog["active_power_built_in_energy"].unit, "W")
        self.assertEqual(
            catalog["active_power_built_in_energy"].device_class, "power"
        )

    def test_compact_tou_json_supports_fourteen_periods(self):
        package_name = "_huawei_emma_tou_test"
        package = types.ModuleType(package_name)
        package.__path__ = []
        sys.modules[package_name] = package
        component_root = Path(__file__).parent / "custom_components" / "huawei_emma_management"
        try:
            for module_name in ("connector_contract", "const", "embedded_catalog", "tou"):
                spec = importlib.util.spec_from_file_location(
                    f"{package_name}.{module_name}", component_root / f"{module_name}.py"
                )
                self.assertIsNotNone(spec)
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                assert spec.loader is not None
                spec.loader.exec_module(module)
            tou = sys.modules[f"{package_name}.tou"]
            periods = [
                {
                    "start_time": index * 60,
                    "end_time": index * 60 + 30,
                    "action": "discharge" if index % 2 else "charge",
                    "days": [True] * 7,
                }
                for index in range(14)
            ]
            encoded = tou.encode_tou_plan_json(periods)
            self.assertLessEqual(len(encoded), 255)
            self.assertEqual(tou.decode_tou_plan_json(encoded), periods)
            self.assertEqual(
                tou.decode_tou_plan_json(
                    '[{"start_time":"00:00","end_time":"04:30","action":"d"}]'
                )[0]["end_time"],
                270,
            )
            with self.assertRaisesRegex(ValueError, "start < end"):
                tou.decode_tou_plan_json('[[270,0,"d"]]')

            luna_text = (
                "00:00-03:59/1234567/+\n"
                "07:00-09:59/1234567/-\n"
                "17:00-20:59/135/-"
            )
            self.assertEqual(
                tou.decode_luna_tou_periods(luna_text),
                [
                    {
                        "start_time": 0,
                        "end_time": 239,
                        "action": "charge",
                        "days": [True] * 7,
                    },
                    {
                        "start_time": 420,
                        "end_time": 599,
                        "action": "discharge",
                        "days": [True] * 7,
                    },
                    {
                        "start_time": 1020,
                        "end_time": 1259,
                        "action": "discharge",
                        "days": [True, False, True, False, True, False, False],
                    },
                ],
            )
            self.assertEqual(tou.decode_luna_tou_periods(""), [])
            decoded_luna = tou.decode_luna_tou_periods(luna_text)
            self.assertEqual(tou.encode_luna_tou_periods(decoded_luna), luna_text)
            self.assertEqual(
                tou.decode_luna_tou_periods(
                    "00:00-03:59/1234567/+ 07:00-09:59/1234567/- "
                    "17:00-20:59/135/-"
                ),
                decoded_luna,
            )
            self.assertEqual(tou.encode_luna_tou_periods([]), "")
            with self.assertRaisesRegex(ValueError, "line 1"):
                tou.decode_luna_tou_periods("00:00-03:59/1234567/x")
            with self.assertRaisesRegex(ValueError, "unique and ordered"):
                tou.decode_luna_tou_periods("00:00-03:59/113/+")
            slots = [
                {
                    "enabled": True,
                    "start_time": 0,
                    "end_time": 360,
                    "mode": "charge",
                    "days": [True] * 7,
                },
                {
                    "enabled": True,
                    "start_time": 360,
                    "end_time": 1439,
                    "mode": "discharge",
                    "batt_mode": "grid_first",
                    "days": [True] * 7,
                },
            ]
            segments = tou.growatt_time_segments(slots)
            self.assertEqual(len(segments), 9)
            self.assertEqual(
                segments[0],
                {
                    "segment_id": 1,
                    "start_time": "00:00",
                    "end_time": "06:00",
                    "batt_mode": "battery_first",
                    "enabled": True,
                },
            )
            self.assertEqual(segments[1]["batt_mode"], "grid_first")
            self.assertFalse(segments[8]["enabled"])
            self.assertEqual(segments[8]["batt_mode"], "load_first")
            self.assertEqual(
                tou.growatt_mode_to_tou_mode("battery_first"), "charge"
            )
            self.assertEqual(tou.growatt_mode_to_tou_mode("grid_first"), "discharge")
            self.assertEqual(tou.parse_time_minutes("06:30:00"), 390)
        finally:
            for module_name in ("tou", "const"):
                sys.modules.pop(f"{package_name}.{module_name}", None)
            sys.modules.pop(package_name, None)

    def test_frame_description_includes_correlated_register_range(self):
        request_pdu = struct.pack(">BHH", 0x03, 31639, 31)
        request = describe_frame(ModbusFrame(7, 0, 0, request_pdu))
        self.assertIn("address=31639 (0x7B97) count=31", request)
        response = describe_frame(
            ModbusFrame(7, 0, 0, b"\x03\x3e" + bytes(62)), request_pdu
        )
        self.assertIn("address=31639 (0x7B97) count=31 byte_count=62", response)
        exception = describe_frame(ModbusFrame(7, 0, 0, b"\x83\x03"), request_pdu)
        self.assertIn("address=31639 (0x7B97) count=31 exception=3", exception)

    def test_live_device_list_page_is_not_misread_as_four_objects(self):
        page = parse_device_identification_page(
            bytes.fromhex("2B 0E 03 03 FF 88 04 87 01 03")
        )
        self.assertTrue(page.more_follows)
        self.assertEqual(page.next_object_id, 0x88)
        self.assertEqual(page.reported_object_count, 4)
        self.assertEqual(page.objects, {0x87: b"\x03"})

    def test_captured_device_list_builds_physical_topology(self):
        devices = parse_topology_devices(
            {
                0x87: b"\x03",
                0x88: b"1=EMMA-A02;2=V100R025C00SPC115;4=TESTEMMA0001;5=0;8=HEMS",
                0x89: b"1=SmartGuard-63A-T0;2=V100R024C00SPC104;4=TESTGUARD001;5=2;8=BackupBox",
                0x8A: b"1=SUN2000-10KTL-M1;2=V100R001C00SPC174;4=TESTINV00001;5=3",
            }
        )
        self.assertEqual([device["role"] for device in devices], ["emma", "smartguard", "inverter"])
        self.assertEqual(devices[2]["unit_id"], 3)
        self.assertNotIn("external_meter", {device["role"] for device in devices})

    def test_device_info_from_captured_startup_shape(self):
        pdu = (
            b"\x41\x44\x01\x0a\x01\x00\x00\x00\x00\x16\x00\x01\x01\x00\x12\xfe"
            b"1=EMMA-A02;2=V100R025C00SPC115;3=P1.15-D1.0;"
            b"4=TESTEMMA0001;5=0;6=1.0;8=HEMS;9=0\x00"
        )
        info = parse_huawei_device_info(pdu)
        self.assertEqual(info["1"], "EMMA-A02")
        self.assertEqual(info["2"], "V100R025C00SPC115")
        self.assertEqual(info["4"], "TESTEMMA0001")

    def test_core_sensor_signed_and_scaled_values(self):
        results = {name: Result(0, None) for name in CORE_REGISTER_NAMES}
        results[rn.PV_OUTPUT_POWER] = Result(5500, "W")
        results[rn.FEED_IN_POWER] = Result(-1000, "W")
        results[rn.STATE_OF_CAPACITY] = Result(75.5, "%")
        states = build_core_sensor_states(results)
        self.assertEqual(states["sensor.huawei_emma_pv_output_power"]["state"], 5.5)
        self.assertEqual(states["sensor.huawei_emma_grid_feed_in_power"]["state"], -1.0)
        self.assertEqual(
            states["sensor.huawei_emma_battery_state_of_capacity"]["state"], 75.5
        )

    def test_catalog_covers_emma_registers_and_control_types(self):
        catalog = build_entity_catalog()
        self.assertEqual(
            len(catalog), sum(register.readable for register in REGISTERS.values())
        )
        self.assertFalse(catalog[str(rn.PN)].enabled_default)
        self.assertEqual(catalog[str(rn.PN)].client_role, "inverter")
        self.assertTrue(
            all(
                description.icon or description.device_class
                for description in catalog.values()
            )
        )
        self.assertGreaterEqual(len(catalog), 100)
        self.assertEqual(catalog[str(rn.EMMA_ESS_CONTROL_MODE)].platform, "select")
        self.assertEqual(catalog[str(rn.EMMA_TOU_PERIODS)].format, "tou_periods")
        self.assertEqual(
            catalog[str(rn.EMMA_TOU_PERIODS)].entity_category, "diagnostic"
        )
        self.assertFalse(catalog[str(rn.EMMA_TOU_PERIODS)].enabled_default)
        self.assertEqual(
            catalog[str(rn.EMMA_MAXIMUM_FEED_GRID_POWER_PERCENT)].platform,
            "number",
        )
        self.assertEqual(
            catalog["emma_external_meter_running_status"].device_role,
            "external_meter",
        )
        self.assertFalse(
            catalog["emma_external_meter_running_status"].enabled_default
        )
        built_in_meter_entities = [
            description
            for name, description in catalog.items()
            if "built_in_energy" in name
        ]
        self.assertGreater(len(built_in_meter_entities), 0)
        self.assertTrue(
            all(description.enabled_default for description in built_in_meter_entities)
        )
        for register_name in (
            rn.PV_OUTPUT_POWER,
            rn.LOAD_POWER,
            rn.FEED_IN_POWER,
            rn.BATTERY_CHARGE_DISCHARGE_POWER,
            rn.INVERTER_RATED_POWER,
            rn.INVERTER_ACTIVE_POWER,
        ):
            self.assertIn(str(register_name), catalog)
        self.assertEqual(catalog[str(rn.EMMA_SYSTEM_TIME)].platform, "datetime")
        self.assertTrue(catalog[str(rn.EMMA_TOU_PERIODS)].writeable)
        self.assertEqual(catalog[str(rn.EMMA_TOU_PERIODS)].format, "tou_periods")
        self.assertEqual(
            catalog[str(rn.STORAGE_MAXIMUM_CHARGING_POWER)].device_role,
            "inverter",
        )
        self.assertEqual(
            register_client_role(str(rn.INVERTER_ENERGY_YIELD_TODAY)), "emma"
        )
        self.assertEqual(
            register_client_role(str(rn.INVERTER_TOTAL_ABSORBED_ENERGY)), "emma"
        )
        self.assertEqual(
            register_client_role(str(rn.INVERTER_RATED_POWER)), "emma"
        )
        self.assertEqual(
            register_client_role(str(rn.STORAGE_MAXIMUM_CHARGING_POWER)),
            "inverter",
        )
        priority = catalog[str(rn.STORAGE_EXCESS_PV_ENERGY_USE_IN_TOU)]
        self.assertEqual(priority.name, "PV Power Priority")
        self.assertEqual(
            {option["label"] for option in priority.options},
            {"Battery First", "Appliances First"},
        )
        for register_name in (
            rn.STORAGE_MAXIMUM_CHARGING_POWER,
            rn.STORAGE_MAXIMUM_DISCHARGING_POWER,
            rn.STORAGE_CHARGING_CUTOFF_CAPACITY,
            rn.STORAGE_DISCHARGING_CUTOFF_CAPACITY,
            rn.STORAGE_GRID_CHARGE_CUTOFF_STATE_OF_CHARGE,
            rn.STORAGE_FORCIBLE_CHARGE_DISCHARGE_SOC,
            rn.STORAGE_FORCIBLE_CHARGE_POWER,
            rn.STORAGE_FORCIBLE_DISCHARGE_POWER,
        ):
            self.assertEqual(catalog[str(register_name)].step, 1)
        self.assertEqual(
            catalog[str(rn.STORAGE_FORCIBLE_CHARGE_POWER)].unit, "W"
        )
        self.assertEqual(
            catalog[str(rn.STORAGE_FORCIBLE_DISCHARGE_POWER)].unit, "W"
        )
        external_controls = [
            description
            for description in catalog.values()
            if description.writeable
            and (
                description.platform in {"select", "switch", "number", "datetime"}
                or description.format == "tou_periods"
            )
        ]
        self.assertGreaterEqual(len(external_controls), 20)
        self.assertNotIn(
            str(rn.PV_OUTPUT_POWER),
            {description.register_name for description in external_controls},
        )

    def test_captured_tou_payload_is_available_as_two_periods(self):
        register_name = str(rn.EMMA_TOU_PERIODS)
        definition = REGISTERS[register_name]
        payload = bytes.fromhex(
            "00 02 00 00 01 0E 01 7F 01 86 05 9F 01 7F" + " 00" * 72
        )
        unpacked = struct.unpack(f">{definition.format}", payload)
        decoded = definition.decode(unpacked)
        states, values = build_catalog_states({register_name: decoded})

        self.assertEqual(len(values[register_name]), 2)
        self.assertEqual(values[register_name][0]["start_time"], 0)
        self.assertEqual(values[register_name][0]["end_time"], 270)
        self.assertEqual(values[register_name][1]["start_time"], 390)
        self.assertEqual(values[register_name][1]["end_time"], 1439)
        self.assertEqual(
            states[f"sensor.huawei_emma_{register_name}"]["state"], 2
        )

    def test_control_conversion_validates_enum_and_tou_periods(self):
        mode_name = str(rn.EMMA_ESS_CONTROL_MODE)
        mode = _convert_register_value(
            ENTITY_CATALOG[mode_name], REGISTERS[mode_name].unit, "maximum_self_consumption"
        )
        self.assertEqual(int(mode), 2)

        tou_name = str(rn.EMMA_TOU_PERIODS)
        periods = _convert_register_value(
            ENTITY_CATALOG[tou_name],
            REGISTERS[tou_name].unit,
            [
                {
                    "start_time": "00:00",
                    "end_time": "06:00",
                    "action": "charge",
                    "days": [True] * 7,
                }
            ],
        )
        self.assertEqual(periods[0].start_time, 0)
        self.assertEqual(periods[0].end_time, 360)
        self.assertEqual(
            _convert_register_value(
                ENTITY_CATALOG[tou_name], REGISTERS[tou_name].unit, []
            ),
            [],
        )
        with self.assertRaisesRegex(ValueError, "seven booleans"):
            _convert_register_value(
                ENTITY_CATALOG[tou_name],
                REGISTERS[tou_name].unit,
                [
                    {
                        "start_time": 0,
                        "end_time": 60,
                        "action": "charge",
                        "days": [True],
                    }
                ],
            )

    def test_environment_token_and_certificates_are_generated(self):
        old_token = os.environ.pop("TEST_EMMA_TOKEN", None)
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token, created = load_or_create_environment(
                    root / ".env", "TEST_EMMA_TOKEN"
                )
                self.assertTrue(created)
                self.assertGreaterEqual(len(token), 32)
                status = ensure_certificates(
                    root / "certs/server-cert.pem",
                    root / "certs/server-key.pem",
                    root / "certs/ca-cert.pem",
                    root / "certs/ca-key.pem",
                    "192.0.2.2",
                )
                self.assertTrue(status.created_ca)
                self.assertTrue(status.created_server)
                sans = status.certificate.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value
                self.assertIn(ipaddress.ip_address("192.0.2.2"), sans.get_values_for_type(x509.IPAddress))
                repeated = ensure_certificates(
                    root / "certs/server-cert.pem",
                    root / "certs/server-key.pem",
                    root / "certs/ca-cert.pem",
                    root / "certs/ca-key.pem",
                    "192.0.2.2",
                )
                self.assertFalse(repeated.created_server)
                renamed = ensure_certificates(
                    root / "certs/server-cert.pem",
                    root / "certs/server-key.pem",
                    root / "certs/ca-cert.pem",
                    root / "certs/ca-key.pem",
                    "emma-new.local",
                )
                self.assertTrue(renamed.created_server)
                renamed_sans = renamed.certificate.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName
                ).value
                self.assertIn(
                    "emma-new.local",
                    renamed_sans.get_values_for_type(x509.DNSName),
                )
                with self.assertRaisesRegex(RuntimeError, "requires existing"):
                    ensure_certificates(
                        root / "missing-cert.pem",
                        root / "missing-key.pem",
                        root / "unused-ca.pem",
                        root / "unused-ca-key.pem",
                        "homeassistant.local",
                        None,
                        False,
                    )
        finally:
            if old_token is None:
                os.environ.pop("TEST_EMMA_TOKEN", None)
            else:
                os.environ["TEST_EMMA_TOKEN"] = old_token


if __name__ == "__main__":
    unittest.main()
