"""End-to-end tests against the simulated EMMA installation.

The real reverse Modbus/TLS server (the same code that runs embedded in Home
Assistant and as the standalone connector) accepts a TLS connection from
tools/emma_mock_client.py, which simulates an EMMA-A02 with a SmartGuard,
a SUN2000 inverter with LUNA battery and optionally an SCharger on separate
unit IDs. These tests verify behaviour against simulated hardware only; they
are not a substitute for validation on a real Huawei installation.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from huawei_modbus_server import (  # noqa: E402
    ReadOnlyModeError,
    ReverseModbusTlsServer,
    ServerConfig,
    WriteVerificationError,
    timestamp_epoch,
)
from tools.emma_mock_client import (  # noqa: E402
    EmmaMockDevice,
    decode_tou_periods,
    run_connection,
)

CLOCK_UTC = 1_767_225_600


class SimulatedEmma:
    def __init__(self, device: EmmaMockDevice, *, disconnect_after: int = 0) -> None:
        self.device = device
        self.disconnect_after = disconnect_after
        self.temp = tempfile.TemporaryDirectory()
        certs = Path(self.temp.name)
        self.server = ReverseModbusTlsServer(
            ServerConfig(
                host="127.0.0.1",
                port=0,
                certfile=certs / "server-cert.pem",
                keyfile=certs / "server-key.pem",
                ca_certfile=certs / "ca-cert.pem",
                ca_keyfile=certs / "ca-key.pem",
                cert_name="127.0.0.1",
                fast_interval=0.2,
                medium_interval=0.4,
                slow_interval=0.6,
                request_timeout=2.0,
            )
        )
        self.task: asyncio.Task | None = None
        self.connections = 0
        self.stopping = False

    @property
    def state(self):
        return self.server.state

    async def __aenter__(self) -> "SimulatedEmma":
        await self.server.async_start()
        port = self.server._server.sockets[0].getsockname()[1]  # noqa: SLF001
        args = SimpleNamespace(
            host="127.0.0.1",
            port=port,
            ca_file=None,
            insecure=True,
            server_name=None,
            local_address=None,
            log_raw=False,
            delay_ms=0,
            disconnect_after=self.disconnect_after,
        )

        async def connect_forever() -> None:
            while not self.stopping:
                self.connections += 1
                try:
                    await run_connection(args, self.device)
                except (OSError, asyncio.IncompleteReadError):
                    pass
                await asyncio.sleep(0.2)

        self.task = asyncio.create_task(connect_forever())
        return self

    async def __aexit__(self, *_exc) -> None:
        # Stop the server first so the simulated EMMA sees EOF and returns;
        # cancelling a task inside an SSL close can otherwise wait forever.
        self.stopping = True
        await self.server.async_stop()
        if self.task is not None:
            done, _pending = await asyncio.wait({self.task}, timeout=3)
            if not done:
                self.task.cancel()
                await asyncio.wait({self.task}, timeout=3)
        self.temp.cleanup()

    async def wait_for(self, predicate, timeout: float = 20.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(
            f"Condition not met; values={sorted(self.state.latest_values)[:40]}"
        )

    async def ready(self) -> None:
        await self.wait_for(
            lambda: "smartguard_status" in self.state.latest_values
            and "emma_tou_periods" in self.state.latest_values
        )


class SimulatedEmmaTests(unittest.IsolatedAsyncioTestCase):
    async def test_smartguard_is_routed_to_its_own_unit(self):
        async with SimulatedEmma(EmmaMockDevice()) as emma:
            emma.state.set_subscriptions(
                ["emma_software_version", "smartguard_software_version", "smartguard_status"]
            )
            await emma.wait_for(
                lambda: "emma_software_version" in emma.state.latest_values
                and "smartguard_software_version" in emma.state.latest_values
            )
            values = emma.state.latest_values
            # 30035 exists on both devices; each value comes from its own unit.
            self.assertEqual(values["emma_software_version"], "V100R025C00SPC115")
            self.assertEqual(values["smartguard_software_version"], "V100R024C00SPC104")
            self.assertEqual(values["smartguard_status"], "on_grid")
            details = emma.state.diagnostics()["devices"]
            self.assertEqual(details["smartguard"]["unit_id"], 2)
            self.assertEqual(details["smartguard"]["phase_type"], "single_phase")
            self.assertTrue(details["smartguard"]["supports_grid_switchover"])
            # Single-phase hardware: phase B/C registers are not polled.
            self.assertIn(
                "smartguard_grid_phase_b_voltage",
                emma.state.states()["not_applicable"],
            )
            # The grid switchover register is never written by the connector.
            self.assertFalse(any(unit == 2 for unit, _a, _v in emma.device.writes))

    async def test_three_phase_smartguard_and_outage(self):
        device = EmmaMockDevice(smartguard_phase="three", grid_outage=True)
        async with SimulatedEmma(device) as emma:
            emma.state.set_subscriptions(
                ["smartguard_status", "smartguard_grid_phase_b_voltage"]
            )
            await emma.wait_for(
                lambda: "smartguard_grid_phase_b_voltage" in emma.state.latest_values
                and "smartguard_status" in emma.state.latest_values
            )
            self.assertEqual(emma.state.latest_values["smartguard_status"], "off_grid_inverter")
            self.assertEqual(emma.state.latest_values["smartguard_grid_phase_b_voltage"], 0)
            self.assertNotIn(
                "smartguard_grid_phase_b_voltage", emma.state.states()["not_applicable"]
            )

    async def test_unknown_enum_value_is_published_raw(self):
        async with SimulatedEmma(EmmaMockDevice(unknown_enum=True)) as emma:
            await emma.wait_for(lambda: "smartguard_status" in emma.state.latest_values)
            self.assertEqual(emma.state.latest_values["smartguard_status"], 99)
            self.assertIn("smartguard_status", emma.state.diagnostics()["registers_raw_values"])

    async def test_verified_writes_and_tou_weekday_order(self):
        async with SimulatedEmma(EmmaMockDevice()) as emma:
            await emma.ready()
            result = await emma.state.set_value("emma_ess_control_mode", 5)
            self.assertIsNotNone(result)
            self.assertEqual(emma.state.diagnostics()["last_verified_write"]["register_name"],
                             "emma_ess_control_mode")

            monday_only = [True, False, False, False, False, False, False]
            periods = [
                {"start_time": "10:00", "end_time": "11:59", "action": "charge", "days": monday_only}
            ]
            readback = await emma.state.set_tou_periods(periods)
            self.assertEqual(readback[0]["days"], monday_only)
            native = decode_tou_periods(emma.device.tou_registers)
            # Huawei's bitmask is Sunday first: Monday is bit 1.
            self.assertEqual(native, [{"start": 600, "end": 719, "charge_flag": 0, "days": 0b10}])
            writes = len(emma.device.writes)
            await emma.state.set_tou_periods(periods)
            self.assertEqual(len(emma.device.writes), writes, "unchanged TOU must not be rewritten")

            with self.assertRaises(ValueError):
                await emma.state.set_value("emma_ess_control_mode", 3)  # not reviewed

    async def test_readback_mismatch_is_reported(self):
        async with SimulatedEmma(EmmaMockDevice(write_failure="ignore")) as emma:
            await emma.ready()
            with self.assertRaises(WriteVerificationError):
                await emma.state.set_value("emma_ess_control_mode", 5)
            self.assertEqual(emma.state.latest_values["emma_ess_control_mode"], "maximum_self_consumption")

    async def test_rejected_write_is_an_error(self):
        async with SimulatedEmma(EmmaMockDevice(write_failure="exception")) as emma:
            await emma.ready()
            with self.assertRaises(Exception) as caught:
                await emma.state.set_value("emma_ess_control_mode", 5)
            self.assertNotIsInstance(caught.exception, WriteVerificationError)

    async def test_read_only_mode_blocks_every_write(self):
        async with SimulatedEmma(EmmaMockDevice()) as emma:
            await emma.ready()
            subscriptions = set(emma.state.subscribed_registers)
            emma.state.set_read_only(True)
            self.assertTrue(emma.state.health()["read_only"])
            self.assertFalse(emma.state.diagnostics()["capabilities"]["writes"])
            with self.assertRaises(ReadOnlyModeError):
                await emma.state.set_value("emma_ess_control_mode", 5)
            with self.assertRaises(ReadOnlyModeError):
                await emma.state.set_tou_periods([])
            session = emma.state.active_session
            with self.assertRaises(ReadOnlyModeError):
                await session.write_single_register(0, 40000, 5)
            self.assertEqual(emma.device.writes, [])
            # Telemetry, discovery and subscriptions continue.
            requests = emma.device.request_count
            await emma.wait_for(lambda: emma.device.request_count > requests + 20)
            self.assertEqual(emma.state.subscribed_registers, subscriptions)
            self.assertIn("smartguard_status", emma.state.latest_values)
            # Switching back restores control without reconnecting.
            emma.state.set_read_only(False)
            await emma.state.set_value("emma_ess_control_mode", 5)
            self.assertEqual(len(emma.device.writes), 1)
            self.assertEqual(emma.connections, 1)

    async def test_read_only_connector_starts_read_only(self):
        device = EmmaMockDevice()
        sim = SimulatedEmma(device)
        sim.server.state.set_read_only(True)
        async with sim as emma:
            await emma.ready()
            self.assertTrue(emma.state.active_session.read_only)
            with self.assertRaises(ReadOnlyModeError):
                await emma.state.set_value("storage_charging_cutoff_capacity", 95)
            self.assertEqual(device.writes, [])

    async def test_local_timestamp_is_normalized_to_utc(self):
        async with SimulatedEmma(EmmaMockDevice()) as emma:
            emma.state.set_subscriptions(["emma_local_time"])
            self.assertIn("emma_system_time", emma.state.subscribed_registers)
            await emma.wait_for(
                lambda: "emma_local_time" in emma.state.latest_values
                and "emma_system_time" in emma.state.latest_values
            )
            values = emma.state.latest_values
            self.assertEqual(values["emma_local_time"], CLOCK_UTC + 7200)  # raw stays raw
            self.assertEqual(timestamp_epoch("emma_local_time", values, "timestamp_local"), CLOCK_UTC)

    async def test_scharger_ipv4_register(self):
        async with SimulatedEmma(EmmaMockDevice(scharger=True)) as emma:
            emma.state.set_subscriptions(["charger_ip_address"])
            await emma.wait_for(lambda: "charger_ip_address" in emma.state.latest_values)
            self.assertEqual(emma.state.latest_values["charger_ip_address"], "192.168.88.40")

    async def test_reconnect_keeps_subscriptions(self):
        async with SimulatedEmma(EmmaMockDevice(), disconnect_after=25) as emma:
            emma.state.set_subscriptions(["smartguard_status", "pv_output_power"])
            subscriptions = set(emma.state.subscribed_registers)
            await emma.wait_for(lambda: emma.state.reconnect_count >= 2, timeout=30)
            await emma.wait_for(lambda: "smartguard_status" in emma.state.latest_values)
            self.assertEqual(emma.state.subscribed_registers, subscriptions)


if __name__ == "__main__":
    unittest.main()
