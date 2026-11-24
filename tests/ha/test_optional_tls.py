"""Optional TLS for the embedded EMMA listener: on by default, plain TCP only by opt-in."""

from __future__ import annotations

from pathlib import Path
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.huawei_emma_management.const import (
    CERTIFICATE_AUTOMATIC,
    CERTIFICATE_CUSTOM,
    CONF_CERTIFICATE_MODE,
    CONF_MODE,
    CONF_TLS_PORT,
    CONF_USE_TLS,
    DOMAIN,
    MODE_EMBEDDED,
)

from test_integration import FakeConnector

def _schema_defaults(result) -> dict:
    return {
        key.schema: key.default()
        for key in result["data_schema"].schema
        if hasattr(key, "default") and callable(key.default)
    }

async def test_setup_offers_tls_enabled_by_default(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_EMBEDDED}
    )
    assert result["step_id"] == "embedded"
    assert _schema_defaults(result)[CONF_USE_TLS] is True

async def test_plain_tcp_skips_custom_certificate_validation(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MODE: MODE_EMBEDDED}
    )
    data = {
        **_schema_defaults(result),
        CONF_USE_TLS: False,
        CONF_TLS_PORT: 16199,
        CONF_CERTIFICATE_MODE: CERTIFICATE_CUSTOM,
    }
    with patch("custom_components.huawei_emma_management.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] == "create_entry"
    assert result["data"][CONF_USE_TLS] is False

async def _setup_embedded(
    hass: HomeAssistant, use_tls: bool | None
) -> tuple[MockConfigEntry, AsyncMock]:
    data = {
        CONF_MODE: MODE_EMBEDDED,
        CONF_TLS_PORT: 0,
        CONF_CERTIFICATE_MODE: CERTIFICATE_AUTOMATIC,
        "certificate_name": "127.0.0.1",
    }
    if use_tls is not None:
        data[CONF_USE_TLS] = use_tls
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, minor_version=2, unique_id="embedded:0", data=data
    )
    entry.add_to_hass(hass)
    listener = MagicMock(sockets=[])
    listener.wait_closed = AsyncMock()
    # Tests may not open sockets; capture how the listener would be started.
    start_server = AsyncMock(return_value=listener)
    # Certificate SAN discovery probes DNS and routes; tests have no network.
    fake_socket = MagicMock()
    fake_socket.gethostname.return_value = "emma-test"
    fake_socket.getaddrinfo.side_effect = OSError
    fake_socket.socket.side_effect = OSError
    with (
        patch(
            "custom_components.huawei_emma_management.embedded_runtime_setup.socket",
            fake_socket,
        ),
        patch(
            "custom_components.huawei_emma_management.EmbeddedEmmaApiClient",
            return_value=FakeConnector(),
        ),
        patch(
            "custom_components.huawei_emma_management.embedded_server.asyncio.start_server",
            start_server,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, start_server

async def test_embedded_plain_tcp_creates_no_certificates(hass: HomeAssistant) -> None:
    entry, start_server = await _setup_embedded(hass, use_tls=False)
    assert start_server.await_args.kwargs["ssl"] is None
    certs = Path(hass.config.path(DOMAIN, entry.entry_id, "certs"))
    assert not (certs / "server-cert.pem").exists()
    assert not (certs / "ca-cert.pem").exists()
    assert await hass.config_entries.async_unload(entry.entry_id)

async def test_embedded_tls_is_default_for_existing_entries(hass: HomeAssistant) -> None:
    # Entries created before 0.15.x have no use_tls key and must keep TLS.
    entry, start_server = await _setup_embedded(hass, use_tls=None)
    assert isinstance(start_server.await_args.kwargs["ssl"], ssl.SSLContext)
    certs = Path(hass.config.path(DOMAIN, entry.entry_id, "certs"))
    assert (certs / "server-cert.pem").exists()
    assert (certs / "ca-cert.pem").exists()
    assert await hass.config_entries.async_unload(entry.entry_id)
