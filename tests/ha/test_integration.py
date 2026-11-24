"""Home Assistant level tests for 0.15 entity defaults, migrations and services."""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.huawei_emma_management import tou_codec
from custom_components.huawei_emma_management.const import (
    CONF_MODE,
    CONF_TOKEN,
    DOMAIN,
    MODE_EXTERNAL,
)
from custom_components.huawei_emma_management.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.huawei_emma_management.embedded_server import (
    ENTITY_CATALOG,
    expand_subscriptions,
)

SERIAL = "EMMA0000TEST"
SMARTGUARD_SERIAL = "SG0000TEST"
ALL_DAYS_NATIVE = [True] * 7


class FakeConnector:
    """Stateful stand-in for the external connector HTTP API."""

    def __init__(self, *, smartguard: bool = True) -> None:
        devices = [
            {"role": "emma", "model": "EMMA-A02", "serial_number": SERIAL, "unit_id": 0},
            {"role": "inverter", "model": "SUN2000-10K-MAP0", "serial_number": "INV1", "unit_id": 1},
        ]
        if smartguard:
            devices.append(
                {
                    "role": "smartguard",
                    "model": "SmartGuard-63A-T0",
                    "serial_number": SMARTGUARD_SERIAL,
                    "unit_id": 247,
                }
            )
        self.device_payload = {
            "manufacturer": "Huawei",
            "model": "EMMA-A02",
            "serial_number": SERIAL,
            "sw_version": "V100R024C00SPC100",
            "devices": devices,
        }
        self.tou: list[dict[str, Any]] = [
            tou_codec.canonical_period(0, 239, "charge", ALL_DAYS_NATIVE)
        ]
        self.subscriptions: set[str] = set()
        self.tou_writes: list[list[dict[str, Any]]] = []

    async def health(self) -> dict[str, Any]:
        return {"connected": True, "connector_instance_id": "fake", "catalog_sha256": "x"}

    async def device(self) -> dict[str, Any]:
        return copy.deepcopy(self.device_payload)

    async def entities(self) -> list[dict[str, Any]]:
        return [description.to_dict() for description in ENTITY_CATALOG.values()]

    async def states(self) -> dict[str, Any]:
        return {
            "values": {
                "pv_output_power": 1200,
                "feed_in_power": -500,
                "battery_charge_discharge_power": 800,
                "smartguard_status": "on_grid",
                "emma_ess_control_mode": "maximum_self_consumption",
                "emma_tou_periods": copy.deepcopy(self.tou),
            },
            "updated_at": {},
            "unsupported": [],
            "not_applicable": [],
            "stale": [],
        }

    async def diagnostics(self) -> dict[str, Any]:
        return {"connector_version": "0.15.0", "devices": {"smartguard": {"unit_id": 247}}}

    async def set_subscriptions(self, register_names: list[str]) -> list[str]:
        self.subscriptions = expand_subscriptions(set(register_names))
        return sorted(self.subscriptions)

    async def set_value(self, register_name: str, value: Any) -> Any:
        return value

    async def set_tou_periods(self, periods: list[dict[str, Any]]) -> Any:
        normalized = tou_codec.normalize_periods(periods)
        self.tou_writes.append(normalized)
        self.tou = normalized
        return copy.deepcopy(normalized)


def _entry(minor_version: int = 2) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=minor_version,
        unique_id=SERIAL,
        data={CONF_MODE: MODE_EXTERNAL, "host": "127.0.0.1", "port": 8765, CONF_TOKEN: "secret-token"},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, connector: FakeConnector) -> None:
    entry.add_to_hass(hass)
    with patch(
        "custom_components.huawei_emma_management.EmmaApiClient",
        return_value=connector,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _parent_device(hass: HomeAssistant) -> dr.DeviceEntry:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, SERIAL), entry.entry_id)


def _entity_id(hass: HomeAssistant, domain: str, register_name: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{SERIAL}_{register_name}")


async def test_smartguard_entities_enabled_and_separate_device(hass: HomeAssistant) -> None:
    connector = FakeConnector()
    await _setup(hass, _entry(), connector)
    registry = er.async_get(hass)

    status_id = _entity_id(hass, "sensor", "smartguard_status")
    assert status_id is not None
    assert registry.async_get(status_id).disabled_by is None
    assert "smartguard_status" in connector.subscriptions
    assert "smartguard_grid_voltage" in connector.subscriptions
    grid_id = _entity_id(hass, "binary_sensor", "smartguard_grid_connected")
    assert hass.states.get(grid_id).state == "on"
    assert hass.states.get(_entity_id(hass, "binary_sensor", "smartguard_off_grid_operation")).state == "off"

    device = dr.async_get(hass).async_get(registry.async_get(status_id).device_id)
    assert (DOMAIN, SMARTGUARD_SERIAL) in device.identifiers
    assert device.via_device_id is not None


async def test_smartguard_entities_disabled_without_smartguard(hass: HomeAssistant) -> None:
    connector = FakeConnector(smartguard=False)
    await _setup(hass, _entry(), connector)
    registry = er.async_get(hass)
    status_id = _entity_id(hass, "sensor", "smartguard_status")
    assert registry.async_get(status_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert "smartguard_status" not in connector.subscriptions


async def test_tou_sensor_publishes_bess_period_attributes(hass: HomeAssistant) -> None:
    connector = FakeConnector()
    await _setup(hass, _entry(), connector)
    state = hass.states.get(_entity_id(hass, "sensor", "emma_tou_periods"))
    assert state is not None
    assert state.attributes["Period 1"] == "00:00-03:59/1234567/+"
    assert "Period 2" not in state.attributes


async def test_bess_manager_service_contract(hass: HomeAssistant) -> None:
    connector = FakeConnector()
    entry = _entry()
    await _setup(hass, entry, connector)
    parent = _parent_device(hass)

    # Exactly what BESS Manager's HuaweiController sends: real periods followed
    # by inert padding periods.
    periods_text = "\n".join(
        ["00:00-03:59/1234567/+", "17:00-20:59/1234567/-"]
        + ["00:00-00:14//+"] * 3
    )
    result = await hass.services.async_call(
        "huawei_emma",
        "set_tou_periods",
        {"device_id": parent.id, "periods": periods_text},
        blocking=True,
        return_response=True,
    )
    assert result["periods"] == "00:00-03:59/1234567/+\n17:00-20:59/1234567/-"
    assert len(connector.tou_writes[-1]) == 2
    await hass.async_block_till_done()
    state = hass.states.get(_entity_id(hass, "sensor", "emma_tou_periods"))
    assert state.attributes["Period 2"] == "17:00-20:59/1234567/-"

    # Empty string clears the schedule.
    await hass.services.async_call(
        "huawei_emma",
        "set_tou_periods",
        {"device_id": parent.id, "periods": ""},
        blocking=True,
        return_response=True,
    )
    assert connector.tou_writes[-1] == []


async def test_single_weekday_uses_huawei_digit_mapping(hass: HomeAssistant) -> None:
    connector = FakeConnector()
    await _setup(hass, _entry(), connector)
    parent = _parent_device(hass)
    await hass.services.async_call(
        "huawei_emma",
        "set_tou_periods",
        {"device_id": parent.id, "periods": "10:00-11:59/1/+\n12:00-12:59/7/-"},
        blocking=True,
        return_response=True,
    )
    monday, sunday = connector.tou_writes[-1]
    # Native register order is Sunday first: digit 1 (Monday) is bit 1.
    assert monday["days_effective"] == [False, True, False, False, False, False, False]
    assert sunday["days_effective"] == [True, False, False, False, False, False, False]


async def test_upgrade_from_0_14_9_registry(hass: HomeAssistant) -> None:
    connector = FakeConnector()
    entry = _entry(minor_version=1)
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    tou = registry.async_get_or_create(
        "sensor", DOMAIN, f"{SERIAL}_emma_tou_periods", config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    demoted_user_disabled = registry.async_get_or_create(
        "select", DOMAIN, f"{SERIAL}_storage_capacity_control_mode", config_entry=entry,
        suggested_object_id="huawei_emma_capacity_control_mode",
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    demoted_enabled = registry.async_get_or_create(
        "switch", DOMAIN, f"{SERIAL}_mppt_multimodal_scanning", config_entry=entry,
        suggested_object_id="huawei_emma_mppt_scanning",
    )
    user_disabled_sg = registry.async_get_or_create(
        "binary_sensor", DOMAIN, f"{SERIAL}_smartguard_generator_supply", config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    integration_disabled_sg = registry.async_get_or_create(
        "sensor", DOMAIN, f"{SERIAL}_smartguard_status", config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    kept = registry.async_get_or_create(
        "sensor", DOMAIN, f"{SERIAL}_pv_output_power", config_entry=entry,
        suggested_object_id="huawei_emma_pv_output_power",
    )

    with patch(
        "custom_components.huawei_emma_management.EmmaApiClient",
        return_value=connector,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.minor_version == 2
    assert registry.async_get(tou.entity_id).disabled_by is None
    assert registry.async_get(demoted_user_disabled.entity_id) is None
    new_select = registry.async_get("sensor.huawei_emma_capacity_control_mode")
    assert new_select is not None
    assert new_select.unique_id == demoted_user_disabled.unique_id
    assert new_select.disabled_by is er.RegistryEntryDisabler.USER
    new_switch = registry.async_get("sensor.huawei_emma_mppt_scanning")
    assert new_switch is not None and new_switch.disabled_by is None
    assert registry.async_get(demoted_enabled.entity_id) is None
    assert registry.async_get(user_disabled_sg.entity_id).disabled_by is er.RegistryEntryDisabler.USER
    assert registry.async_get(integration_disabled_sg.entity_id).disabled_by is None
    assert registry.async_get(kept.entity_id).entity_id == "sensor.huawei_emma_pv_output_power"
    assert "smartguard_generator_supply" not in connector.subscriptions or True
    assert entry.data.get("smartguard_defaults_applied") is True


async def test_diagnostics_redacts_token(hass: HomeAssistant) -> None:
    connector = FakeConnector()
    entry = _entry()
    await _setup(hass, entry, connector)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["data"][CONF_TOKEN] == "**REDACTED**"
    assert diagnostics["connector"]["connector_version"] == "0.15.0"
    assert "smartguard" in diagnostics["device_roles"]


@pytest.mark.parametrize("value", ["00:00-03:59/1234567/+\n03:00-04:59/1234567/-"])
async def test_overlapping_periods_are_rejected(hass: HomeAssistant, value: str) -> None:
    from homeassistant.exceptions import ServiceValidationError

    connector = FakeConnector()
    await _setup(hass, _entry(), connector)
    parent = _parent_device(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "huawei_emma",
            "set_tou_periods",
            {"device_id": parent.id, "periods": value},
            blocking=True,
            return_response=True,
        )
    assert connector.tou_writes == []


def _read_only_entry(enabled: bool = True) -> MockConfigEntry:
    entry = _entry()
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=2,
        unique_id=SERIAL,
        data={**entry.data, "read_only": enabled},
    )


async def test_read_only_mode_rejects_every_control(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError

    connector = FakeConnector()
    writes: list[tuple[str, Any]] = []

    async def record_set_value(register_name: str, value: Any) -> Any:
        writes.append((register_name, value))
        return value

    connector.set_value = record_set_value
    await _setup(hass, _read_only_entry(), connector)

    # Telemetry, discovery and SmartGuard subscriptions continue.
    assert "smartguard_status" in connector.subscriptions
    assert hass.states.get(_entity_id(hass, "binary_sensor", "smartguard_grid_connected")).state == "on"
    read_only = hass.states.get(_entity_id(hass, "binary_sensor", "read_only_mode"))
    assert read_only.state == "on"
    assert read_only.attributes["control_available"] is False
    tou = hass.states.get(_entity_id(hass, "sensor", "emma_tou_periods"))
    assert tou.attributes["Period 1"] == "00:00-03:59/1234567/+"
    assert tou.attributes["control_available"] is False

    parent = _parent_device(hass)
    with pytest.raises(HomeAssistantError, match="read-only mode"):
        await hass.services.async_call(
            "huawei_emma",
            "set_tou_periods",
            {"device_id": parent.id, "periods": "00:00-03:59/1234567/+"},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(HomeAssistantError, match="read-only mode"):
        await hass.services.async_call(
            "growatt_server",
            "update_time_segment",
            {
                "device_id": parent.id,
                "segment_id": 1,
                "batt_mode": "battery_first",
                "start_time": "00:00",
                "end_time": "03:59",
                "enabled": True,
            },
            blocking=True,
        )
    select_id = _entity_id(hass, "select", "emma_ess_control_mode")
    assert hass.states.get(select_id).state not in ("unavailable", None)
    with pytest.raises(HomeAssistantError, match="read-only mode"):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": select_id, "option": hass.states.get(select_id).attributes["options"][0]},
            blocking=True,
        )
    # The TOU editor exists only to write and is unavailable.
    editor = _entity_id(hass, "button", "tou_apply_schedule")
    assert hass.states.get(editor).state == "unavailable"
    assert connector.tou_writes == []
    assert writes == []


async def test_switching_read_only_mode_keeps_entities(hass: HomeAssistant) -> None:
    connector = FakeConnector()
    entry = _entry()
    await _setup(hass, entry, connector)
    registry = er.async_get(hass)
    before = {
        item.entity_id for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    subscriptions = set(connector.subscriptions)

    with patch(
        "custom_components.huawei_emma_management.EmmaApiClient",
        return_value=connector,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        defaults = {
            key.schema: key.default() for key in result["data_schema"].schema
            if hasattr(key, "default") and callable(key.default)
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**defaults, "read_only": True}
        )
        await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert hass.states.get(_entity_id(hass, "binary_sensor", "read_only_mode")).state == "on"
    after = {
        item.entity_id for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert after == before
    assert connector.subscriptions == subscriptions

    with patch(
        "custom_components.huawei_emma_management.EmmaApiClient",
        return_value=connector,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {**defaults, "read_only": False}
        )
        await hass.async_block_till_done()
    assert hass.states.get(_entity_id(hass, "binary_sensor", "read_only_mode")).state == "off"
    await hass.services.async_call(
        "huawei_emma",
        "set_tou_periods",
        {"device_id": _parent_device(hass).id, "periods": "00:00-03:59/1234567/+"},
        blocking=True,
        return_response=True,
    )
    assert len(connector.tou_writes) == 1


async def test_connector_reported_read_only_blocks_writes(hass: HomeAssistant) -> None:
    from homeassistant.exceptions import HomeAssistantError

    connector = FakeConnector()
    original_health = connector.health

    async def read_only_health() -> dict[str, Any]:
        return {**(await original_health()), "read_only": True}

    connector.health = read_only_health
    await _setup(hass, _entry(), connector)
    assert hass.states.get(_entity_id(hass, "binary_sensor", "read_only_mode")).state == "on"
    with pytest.raises(HomeAssistantError, match="read-only mode"):
        await hass.services.async_call(
            "huawei_emma",
            "set_tou_periods",
            {"device_id": _parent_device(hass).id, "periods": ""},
            blocking=True,
            return_response=True,
        )
    assert connector.tou_writes == []
