from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EmmaApiClient, EmmaApiError
from .const import (
    CONF_DISCOVERED_DEVICE,
    DOMAIN,
    GROWATT_MAX_SEGMENTS,
    TOU_MAX_PERIODS,
    TOU_MODES,
    TOU_REGISTER_NAME,
)
from .tou import (
    growatt_mode_to_tou_mode,
    growatt_time_segments,
    parse_time_minutes,
    tou_mode_to_growatt_mode,
)


_LOGGER = logging.getLogger(__name__)


class EmmaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(
        self,
        hass: HomeAssistant,
        api: EmmaApiClient,
        *,
        entry_id: str | None = None,
        initial_device_data: dict[str, Any] | None = None,
        stable_identifier: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=10),
        )
        self.api = api
        self.embedded_server: Any | None = None
        self.entry_id = entry_id
        self.stable_identifier = stable_identifier
        self.accept_external_growatt_controls = True
        self.device_data: dict[str, Any] = dict(initial_device_data or {})
        self.entity_descriptions: list[dict[str, Any]] = []
        self.tou_slots = [self._new_tou_slot() for _ in range(TOU_MAX_PERIODS)]
        self.growatt_slots = [
            self._new_growatt_slot() for _ in range(GROWATT_MAX_SEGMENTS)
        ]
        self._growatt_tail_slots: list[dict[str, Any]] = []
        self._growatt_slots_initialized = False
        self._growatt_active_fingerprint: tuple[Any, ...] | None = None
        self._tou_write_lock = asyncio.Lock()
        self._subscription_sync_lock = asyncio.Lock()
        self._accepted_subscriptions: set[str] | None = None
        self.selected_tou_slot = 0
        self.tou_dirty = False
        self._discovery_reload_scheduled = False

    @property
    def parent_identifier(self) -> str:
        return str(
            self.stable_identifier
            or self.device_data.get("serial_number")
            or self.device_data.get("model")
            or "emma"
        )

    @property
    def unique_id_prefix(self) -> str:
        return self.parent_identifier

    async def async_initialize(self) -> None:
        discovered = await self.api.device()
        if discovered.get("serial_number") or not self.device_data:
            self.device_data = discovered
        self.entity_descriptions = await self.api.entities()
        await self.async_sync_polling_subscriptions(source="initial_setup")
        await self.async_config_entry_first_refresh()

    async def async_sync_polling_subscriptions(
        self, *, source: str = "entity_registry"
    ) -> None:
        """Make the connector poll exactly the entities enabled in HA."""
        async with self._subscription_sync_lock:
            registry = er.async_get(self.hass)
            enabled: set[str] = set()
            for description in self.entity_descriptions:
                register_name = description["register_name"]
                unique_id = f"{self.unique_id_prefix}_{register_name}"
                entity_id = registry.async_get_entity_id(
                    description["platform"], DOMAIN, unique_id
                )
                registry_entry = registry.async_get(entity_id) if entity_id else None
                if (
                    registry_entry is not None
                    and registry_entry.disabled_by is None
                ) or (
                    registry_entry is None
                    and description.get("enabled_default", True)
                ):
                    enabled.add(
                        description.get("source_register_name", register_name)
                    )

            # TOU powers the two schedule sensors, editor, Growatt compatibility,
            # and native services, so it remains an internal required subscription.
            enabled.add(TOU_REGISTER_NAME)
            try:
                accepted = set(await self.api.set_subscriptions(sorted(enabled)))
            except EmmaApiError as error:
                # Keep compatibility with a connector that has not yet been updated;
                # it will continue using its own default poll set.
                _LOGGER.warning(
                    "Connector does not support dynamic polling subscriptions yet: %s",
                    error,
                )
                return
            previous = self._accepted_subscriptions
            self._accepted_subscriptions = accepted
            if previous is None:
                _LOGGER.info(
                    "Synchronized Home Assistant polling subscriptions source=%s "
                    "enabled=%d catalog=%d",
                    source,
                    len(accepted),
                    len(self.entity_descriptions),
                )
                return
            added = sorted(accepted - previous)
            removed = sorted(previous - accepted)
            _LOGGER.info(
                "Synchronized Home Assistant polling subscriptions source=%s "
                "total=%d added=%d removed=%d",
                source,
                len(accepted),
                len(added),
                len(removed),
            )
            if added:
                _LOGGER.info("Polling subscriptions added: %s", ", ".join(added))
            if removed:
                _LOGGER.info("Polling subscriptions removed: %s", ", ".join(removed))

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            health = await self.api.health()
            states = await self.api.states()
            discovered = await self.api.device() if health.get("connected") else None
        except EmmaApiError as error:
            raise UpdateFailed(str(error)) from error
        if discovered is not None:
            if discovered.get("serial_number"):
                old_fingerprint = self._device_fingerprint(self.device_data)
                new_fingerprint = self._device_fingerprint(discovered)
                self.device_data = discovered
                if (
                    self.entry_id
                    and discovered.get("devices")
                    and new_fingerprint != old_fingerprint
                    and not self._discovery_reload_scheduled
                ):
                    entry = self.hass.config_entries.async_get_entry(self.entry_id)
                    if entry is not None:
                        self.hass.config_entries.async_update_entry(
                            entry,
                            data={
                                **entry.data,
                                CONF_DISCOVERED_DEVICE: discovered,
                            },
                        )
                        self._discovery_reload_scheduled = True
                        _LOGGER.info(
                            "Discovered embedded EMMA topology; reloading entry to "
                            "assign stable devices and entity IDs"
                        )
                        entry.async_create_task(
                            self.hass,
                            self.hass.config_entries.async_reload(self.entry_id),
                            "reload Huawei EMMA after topology discovery",
                        )
        data = {"health": health, **states}
        if not self.tou_dirty:
            periods = data.get("values", {}).get(TOU_REGISTER_NAME)
            if isinstance(periods, list):
                self._load_tou_slots(periods)
        return data

    @staticmethod
    def _device_fingerprint(device: dict[str, Any]) -> tuple[Any, ...]:
        return (
            device.get("model"),
            device.get("serial_number"),
            device.get("sw_version"),
            tuple(
                (
                    item.get("role"),
                    item.get("model"),
                    item.get("serial_number"),
                    item.get("sw_version"),
                )
                for item in device.get("devices", [])
                if isinstance(item, dict)
            ),
        )

    async def async_set_value(
        self,
        register_name: str,
        value: Any,
        *,
        source: str = "home_assistant_entity",
    ) -> Any:
        _LOGGER.debug(
            "CONTROL received source=%s register=%s requested_value=%r",
            source,
            register_name,
            value,
        )
        try:
            value = await self.api.set_value(register_name, value)
        except EmmaApiError as error:
            _LOGGER.debug(
                "CONTROL rejected source=%s register=%s reason=%s",
                source,
                register_name,
                error,
            )
            raise
        _LOGGER.debug(
            "CONTROL accepted source=%s register=%s normalized_value=%r; "
            "refreshing state",
            source,
            register_name,
            value,
        )
        await self.async_request_refresh()
        _LOGGER.debug(
            "CONTROL completed source=%s register=%s current_value=%r",
            source,
            register_name,
            self.data.get("values", {}).get(register_name) if self.data else None,
        )
        return value

    @staticmethod
    def _new_tou_slot() -> dict[str, Any]:
        return {
            "enabled": False,
            "start_time": 0,
            "end_time": 0,
            "mode": "charge",
            "days": [True] * 7,
        }

    @classmethod
    def _new_growatt_slot(cls) -> dict[str, Any]:
        slot = cls._new_tou_slot()
        slot.update({"mode": "discharge", "batt_mode": "load_first"})
        return slot

    def _load_tou_slots(
        self,
        periods: list[dict[str, Any]],
        *,
        sync_growatt: bool = False,
        preserve_growatt: bool = False,
    ) -> None:
        slots = [self._new_tou_slot() for _ in range(TOU_MAX_PERIODS)]
        for index, period in enumerate(periods[:TOU_MAX_PERIODS]):
            if not isinstance(period, dict):
                continue
            mode = period.get("action", period.get("charge_flag", "charge"))
            if isinstance(mode, int):
                mode = "discharge" if mode == 1 else "charge"
            mode = str(mode).lower()
            if mode not in TOU_MODES:
                mode = "charge"
            days = period.get("days", period.get("days_effective", [True] * 7))
            if not isinstance(days, list) or len(days) != 7:
                days = [True] * 7
            slots[index] = {
                "enabled": True,
                "start_time": int(period.get("start_time", 0)),
                "end_time": int(period.get("end_time", 0)),
                "mode": mode,
                "days": [bool(day) for day in days],
            }
        self.tou_slots = slots
        fingerprint = self._tou_fingerprint(slots)
        active_changed = fingerprint != self._growatt_active_fingerprint
        if active_changed and self._growatt_active_fingerprint is not None:
            _LOGGER.debug(
                "TOU active schedule changed periods=%r preserve_growatt=%s "
                "force_sync_growatt=%s",
                periods,
                preserve_growatt,
                sync_growatt,
            )
        if (
            sync_growatt
            or not self._growatt_slots_initialized
            or (active_changed and not preserve_growatt)
        ):
            self._sync_growatt_slots(slots)
        self._growatt_active_fingerprint = fingerprint

    @staticmethod
    def _tou_fingerprint(slots: list[dict[str, Any]]) -> tuple[Any, ...]:
        return tuple(
            (
                slot["start_time"],
                slot["end_time"],
                slot["mode"],
                tuple(slot["days"]),
            )
            for slot in slots
            if slot["enabled"]
        )

    def _sync_growatt_slots(self, slots: list[dict[str, Any]]) -> None:
        compatibility_slots = [
            self._new_growatt_slot() for _ in range(GROWATT_MAX_SEGMENTS)
        ]
        for index, slot in enumerate(slots[:GROWATT_MAX_SEGMENTS]):
            compatibility_slots[index] = {
                **slot,
                "days": list(slot["days"]),
                "batt_mode": tou_mode_to_growatt_mode(slot["mode"]),
            }
        self.growatt_slots = compatibility_slots
        self._growatt_tail_slots = [
            {**slot, "days": list(slot["days"])}
            for slot in slots[GROWATT_MAX_SEGMENTS:]
            if slot["enabled"]
        ]
        self._growatt_slots_initialized = True

    def select_tou_slot(self, index: int) -> None:
        if not 0 <= index < TOU_MAX_PERIODS:
            raise ValueError(f"TOU period must be between 1 and {TOU_MAX_PERIODS}")
        previous = self.selected_tou_slot
        self.selected_tou_slot = index
        _LOGGER.debug(
            "TOU editor selected period old=%s new=%s",
            previous + 1,
            index + 1,
        )
        self.async_update_listeners()

    def update_selected_tou_slot(self, **changes: Any) -> None:
        before = dict(self.tou_slots[self.selected_tou_slot])
        self.tou_slots[self.selected_tou_slot].update(changes)
        self.tou_dirty = True
        _LOGGER.debug(
            "TOU editor changed period=%s changes=%r before=%r after=%r "
            "validation=pending_apply",
            self.selected_tou_slot + 1,
            changes,
            before,
            self.tou_slots[self.selected_tou_slot],
        )
        self.async_update_listeners()

    def clear_selected_tou_slot(self) -> None:
        previous = self.tou_slots[self.selected_tou_slot]
        self.tou_slots[self.selected_tou_slot] = self._new_tou_slot()
        self.tou_dirty = True
        _LOGGER.debug(
            "TOU editor cleared period=%s previous=%r action=draft_only",
            self.selected_tou_slot + 1,
            previous,
        )
        self.async_update_listeners()

    def planned_tou_periods(self) -> list[dict[str, Any]]:
        return [
            {
                "start_time": slot["start_time"],
                "end_time": slot["end_time"],
                "action": slot["mode"],
                "days": list(slot["days"]),
            }
            for slot in self.tou_slots
            if slot["enabled"]
        ]

    def replace_tou_plan(self, periods: list[dict[str, Any]]) -> None:
        _LOGGER.debug(
            "TOU JSON plan validated periods=%r action=replace_draft", periods
        )
        self._load_tou_slots(periods, preserve_growatt=True)
        self.tou_dirty = True
        self.async_update_listeners()

    async def async_apply_tou_schedule(self) -> None:
        await self.async_set_tou_schedule(
            self.planned_tou_periods(), source="home_assistant_tou_apply_button"
        )

    async def async_set_tou_schedule(
        self,
        periods: list[dict[str, Any]],
        *,
        source: str = "home_assistant_service",
    ) -> list[dict[str, Any]]:
        _LOGGER.debug(
            "TOU WRITE received source=%s periods=%r validation=connector_pending",
            source,
            periods,
        )
        try:
            async with self._tou_write_lock:
                value = await self.api.set_tou_periods(periods)
                _LOGGER.debug(
                    "TOU WRITE connector accepted source=%s requested=%r "
                    "readback=%r",
                    source,
                    periods,
                    value,
                )
                self.tou_dirty = False
                self._load_tou_slots(
                    value if isinstance(value, list) else periods,
                    sync_growatt=True,
                )
        except EmmaApiError as error:
            _LOGGER.debug(
                "TOU WRITE rejected source=%s periods=%r reason=%s",
                source,
                periods,
                error,
            )
            raise
        await self.async_request_refresh()
        _LOGGER.debug(
            "TOU WRITE completed source=%s active_periods=%r",
            source,
            self.data.get("values", {}).get(TOU_REGISTER_NAME)
            if self.data
            else None,
        )
        active_periods = (
            self.data.get("values", {}).get(TOU_REGISTER_NAME) if self.data else None
        )
        if isinstance(active_periods, list):
            return active_periods
        return value if isinstance(value, list) else periods

    def growatt_time_segments(self) -> list[dict[str, Any]]:
        """Return the nine fixed slots expected by the Growatt action contract."""
        return growatt_time_segments(self.growatt_slots)

    async def async_update_growatt_time_segment(
        self,
        segment_id: int,
        batt_mode: str,
        start_time: str,
        end_time: str,
        enabled: bool,
    ) -> None:
        """Update one compatibility slot and immediately commit the EMMA schedule."""
        _LOGGER.debug(
            "GROWATT UPDATE received segment_id=%s batt_mode=%s start=%s end=%s "
            "enabled=%s",
            segment_id,
            batt_mode,
            start_time,
            end_time,
            enabled,
        )
        if not 1 <= segment_id <= GROWATT_MAX_SEGMENTS:
            raise ValueError(
                f"segment_id must be between 1 and {GROWATT_MAX_SEGMENTS}"
            )
        start = parse_time_minutes(start_time, "start_time")
        end = parse_time_minutes(end_time, "end_time")
        if not 0 <= start <= 1439 or not 0 <= end <= 1439:
            raise ValueError("start_time and end_time must be between 00:00 and 23:59")
        if enabled and not start < end:
            raise ValueError(
                "An enabled time segment must have start_time before end_time"
            )

        mode = growatt_mode_to_tou_mode(batt_mode)
        _LOGGER.debug(
            "GROWATT UPDATE validated segment_id=%s parsed_start=%s parsed_end=%s "
            "mapped_emma_mode=%s action=replace_slot_and_write_schedule",
            segment_id,
            start,
            end,
            mode,
        )
        if batt_mode == "grid_first":
            _LOGGER.warning(
                "Growatt grid_first segment %s maps to EMMA discharge; EMMA TOU "
                "cannot distinguish grid_first from load_first",
                segment_id,
            )
        async with self._tou_write_lock:
            previous_slot = self.growatt_slots[segment_id - 1]
            self.growatt_slots[segment_id - 1] = {
                "enabled": enabled,
                "start_time": start,
                "end_time": end,
                "mode": mode,
                "batt_mode": batt_mode,
                "days": [True] * 7,
            }
            periods = [
                {
                    "start_time": slot["start_time"],
                    "end_time": slot["end_time"],
                    "action": slot["mode"],
                    "days": list(slot["days"]),
                }
                for slot in [*self.growatt_slots, *self._growatt_tail_slots]
                if slot["enabled"]
            ]
            try:
                value = await self.api.set_tou_periods(periods)
            except EmmaApiError as error:
                self.growatt_slots[segment_id - 1] = previous_slot
                _LOGGER.debug(
                    "GROWATT UPDATE rejected segment_id=%s generated_periods=%r "
                    "rollback=true reason=%s",
                    segment_id,
                    periods,
                    error,
                )
                raise
            _LOGGER.debug(
                "GROWATT UPDATE connector accepted segment_id=%s generated_periods=%r "
                "readback=%r",
                segment_id,
                periods,
                value,
            )
            self.tou_dirty = False
            self._load_tou_slots(
                value if isinstance(value, list) else periods,
                preserve_growatt=True,
            )
        await self.async_request_refresh()
        _LOGGER.debug(
            "GROWATT UPDATE completed segment_id=%s compatibility_segments=%r "
            "active_emma_periods=%r",
            segment_id,
            self.growatt_time_segments(),
            self.data.get("values", {}).get(TOU_REGISTER_NAME)
            if self.data
            else None,
        )
