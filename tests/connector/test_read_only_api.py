"""Read-only mode at the connector API level (Python and ESPHome parity)."""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from huawei_modbus_server import (  # noqa: E402
    MODBUS_WRITE_FUNCTION_CODES,
    CommandApi,
    RuntimeState,
)


async def _request(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps(body).encode() if body is not None else b""
    writer.write(
        f"{method} {path} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer token\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )
    await writer.drain()
    response = await reader.read()
    writer.close()
    head, _, data = response.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(data)


class ReadOnlyApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = RuntimeState(read_only=True)
        self.server = await asyncio.start_server(
            CommandApi(self.state, "token").handle, "127.0.0.1", 0
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def test_writes_are_locked_but_reads_and_subscriptions_work(self):
        status, body = await _request(
            self.port, "POST", "/api/v1/entities/emma_ess_control_mode/value", {"value": 5}
        )
        self.assertEqual(status, 423)
        self.assertIn("read-only mode", body["error"])
        status, _ = await _request(self.port, "POST", "/api/v1/tou-periods", {"periods": []})
        self.assertEqual(status, 423)
        status, _ = await _request(self.port, "POST", "/commands/ess_control_mode", {"value": 5})
        self.assertEqual(status, 423)
        status, body = await _request(
            self.port, "POST", "/api/v1/subscriptions", {"register_names": ["smartguard_status"]}
        )
        self.assertEqual(status, 200)
        self.assertIn("smartguard_status", body["register_names"])
        status, health = await _request(self.port, "GET", "/api/v1/health")
        self.assertEqual((status, health["read_only"]), (200, True))
        status, diagnostics = await _request(self.port, "GET", "/api/v1/diagnostics")
        self.assertTrue(diagnostics["read_only"])
        self.assertFalse(diagnostics["control_available"])
        self.assertFalse(diagnostics["capabilities"]["writes"])

    async def test_switching_modes_without_restart(self):
        self.state.set_read_only(False)
        status, _ = await _request(
            self.port, "POST", "/api/v1/entities/emma_ess_control_mode/value", {"value": 5}
        )
        self.assertNotEqual(status, 423)  # not locked; EMMA is simply not connected
        self.state.set_read_only(True)
        status, _ = await _request(
            self.port, "POST", "/api/v1/entities/emma_ess_control_mode/value", {"value": 5}
        )
        self.assertEqual(status, 423)


class ReadOnlyParityTests(unittest.TestCase):
    def test_esphome_blocks_the_same_function_codes(self):
        source = (
            ROOT / "esphome/components/huawei_emma_reverse/huawei_emma_reverse.cpp"
        ).read_text(encoding="utf-8")
        schema = (ROOT / "esphome/components/huawei_emma_reverse/__init__.py").read_text(
            encoding="utf-8"
        )
        for code in MODBUS_WRITE_FUNCTION_CODES:
            self.assertIn(f"pdu[0] == 0x{code:02X}", source)
        self.assertIn('"423 Locked"', source)
        self.assertIn("CONF_READ_ONLY", schema)
        self.assertIn('\\"read_only\\":', source)


if __name__ == "__main__":
    unittest.main()
