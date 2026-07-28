"""Config flow for embedded and external Huawei EMMA connector modes."""

from __future__ import annotations

import logging
from pathlib import Path
import secrets
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    EmmaApiAuthError,
    EmmaApiClient,
    EmmaApiConnectionError,
    EmmaApiError,
)
from .const import (
    CERTIFICATE_AUTOMATIC,
    CERTIFICATE_CUSTOM,
    CONF_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
    CONF_CERTIFICATE_MODE,
    CONF_CERTIFICATE_NAME,
    CONF_CERTIFICATE_PATH,
    CONF_MODE,
    CONF_PRIVATE_KEY_PASSWORD,
    CONF_PRIVATE_KEY_PATH,
    CONF_TLS_PORT,
    CONF_TOKEN,
    DEFAULT_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
    DEFAULT_PORT,
    DEFAULT_TLS_PORT,
    DOMAIN,
    MODE_EMBEDDED,
    MODE_EXTERNAL,
)
from .embedded_runtime_setup import ensure_certificates


_LOGGER = logging.getLogger(__name__)


def _config_path(hass: Any, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(hass.config.path(value))


class HuaweiEmmaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._external_token = secrets.token_urlsafe(32)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return HuaweiEmmaOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            if user_input[CONF_MODE] == MODE_EMBEDDED:
                return await self.async_step_embedded()
            return await self.async_step_external()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MODE, default=MODE_EMBEDDED): vol.In(
                        (MODE_EMBEDDED, MODE_EXTERNAL)
                    )
                }
            ),
        )

    async def async_step_embedded(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_CERTIFICATE_MODE] == CERTIFICATE_CUSTOM:
                cert_path = _config_path(
                    self.hass, user_input.get(CONF_CERTIFICATE_PATH, "")
                )
                key_path = _config_path(
                    self.hass, user_input.get(CONF_PRIVATE_KEY_PATH, "")
                )
                if not user_input.get(CONF_CERTIFICATE_PATH) or not user_input.get(
                    CONF_PRIVATE_KEY_PATH
                ):
                    errors["base"] = "certificate_paths_required"
                elif not cert_path.is_file() or not key_path.is_file():
                    errors["base"] = "invalid_certificate"
                else:
                    validation_dir = Path(
                        self.hass.config.path(DOMAIN, "certificate-validation")
                    )
                    try:
                        await self.hass.async_add_executor_job(
                            ensure_certificates,
                            cert_path,
                            key_path,
                            validation_dir / "unused-ca-cert.pem",
                            validation_dir / "unused-ca-key.pem",
                            user_input.get(CONF_CERTIFICATE_NAME) or None,
                            user_input.get(CONF_PRIVATE_KEY_PASSWORD) or None,
                            False,
                        )
                    except (OSError, RuntimeError, ValueError) as error:
                        _LOGGER.debug("Custom TLS validation failed: %s", error)
                        errors["base"] = "invalid_certificate"
            if not errors:
                await self.async_set_unique_id(
                    f"embedded:{user_input[CONF_TLS_PORT]}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title="Huawei EMMA (Embedded)",
                    data={CONF_MODE: MODE_EMBEDDED, **user_input},
                )

        schema = vol.Schema(
            {
                    vol.Required(CONF_TLS_PORT, default=DEFAULT_TLS_PORT): cv.port,
                    vol.Required(
                        CONF_CERTIFICATE_MODE, default=CERTIFICATE_AUTOMATIC
                    ): vol.In((CERTIFICATE_AUTOMATIC, CERTIFICATE_CUSTOM)),
                    vol.Optional(
                        CONF_CERTIFICATE_NAME,
                        default="homeassistant.local",
                    ): str,
                    vol.Optional(CONF_CERTIFICATE_PATH, default=""): str,
                    vol.Optional(CONF_PRIVATE_KEY_PATH, default=""): str,
                    vol.Optional(CONF_PRIVATE_KEY_PASSWORD, default=""): str,
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="embedded",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_external(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        if user_input is not None:
            api = EmmaApiClient(
                async_get_clientsession(self.hass),
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                user_input[CONF_TOKEN],
            )
            try:
                device = await api.device()
                health = await api.health()
                if not health.get("connected"):
                    raise EmmaApiError("EMMA is not currently connected")
            except EmmaApiAuthError:
                errors["base"] = "invalid_auth"
            except (EmmaApiConnectionError, EmmaApiError):
                errors["base"] = "cannot_connect"
            else:
                unique_id = device.get("serial_number") or (
                    f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}"
                )
                await self.async_set_unique_id(str(unique_id))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Huawei {device.get('model') or 'EMMA'}",
                    data={CONF_MODE: MODE_EXTERNAL, **user_input},
                )

        schema = vol.Schema(
            {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
                    vol.Required(CONF_TOKEN, default=self._external_token): str,
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="external",
            data_schema=schema,
            errors=errors,
        )


class HuaweiEmmaOptionsFlow(config_entries.OptionsFlowWithReload):
    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        mode = self.config_entry.data.get(CONF_MODE, MODE_EXTERNAL)
        errors: dict[str, str] = {}
        if user_input is not None:
            if mode == MODE_EMBEDDED and user_input.get(
                CONF_CERTIFICATE_MODE
            ) == CERTIFICATE_CUSTOM:
                cert_value = user_input.get(CONF_CERTIFICATE_PATH, "")
                key_value = user_input.get(CONF_PRIVATE_KEY_PATH, "")
                cert_path = _config_path(self.hass, cert_value)
                key_path = _config_path(self.hass, key_value)
                if not cert_value or not key_value:
                    errors["base"] = "certificate_paths_required"
                elif not cert_path.is_file() or not key_path.is_file():
                    errors["base"] = "invalid_certificate"
                else:
                    validation_dir = Path(
                        self.hass.config.path(DOMAIN, "certificate-validation")
                    )
                    try:
                        await self.hass.async_add_executor_job(
                            ensure_certificates,
                            cert_path,
                            key_path,
                            validation_dir / "unused-ca-cert.pem",
                            validation_dir / "unused-ca-key.pem",
                            user_input.get(CONF_CERTIFICATE_NAME) or None,
                            user_input.get(CONF_PRIVATE_KEY_PASSWORD) or None,
                            False,
                        )
                    except (OSError, RuntimeError, ValueError) as error:
                        _LOGGER.debug("Custom TLS option validation failed: %s", error)
                        errors["base"] = "invalid_certificate"
            if errors:
                pass
            else:
                updated_data = dict(self.config_entry.data)
                if mode == MODE_EMBEDDED:
                    for key in (
                        CONF_TLS_PORT,
                        CONF_CERTIFICATE_MODE,
                        CONF_CERTIFICATE_NAME,
                        CONF_CERTIFICATE_PATH,
                        CONF_PRIVATE_KEY_PATH,
                        CONF_PRIVATE_KEY_PASSWORD,
                    ):
                        updated_data[key] = user_input.pop(key)
                else:
                    updated_data[CONF_HOST] = user_input.pop(CONF_HOST)
                    updated_data[CONF_PORT] = user_input.pop(CONF_PORT)
                    updated_data[CONF_TOKEN] = user_input.pop(CONF_TOKEN)
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=updated_data
                )
                _LOGGER.debug(
                    "CONFIG option changed entry=%s mode=%s "
                    "accept_external_growatt_controls=%s validation=accepted "
                    "action=save_and_reload",
                    self.config_entry.entry_id,
                    mode,
                    user_input[CONF_ACCEPT_EXTERNAL_GROWATT_CONTROLS],
                )
                return self.async_create_entry(data=user_input)

        fields: dict[Any, Any] = {
            vol.Required(
                CONF_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
                default=self.config_entry.options.get(
                    CONF_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
                    DEFAULT_ACCEPT_EXTERNAL_GROWATT_CONTROLS,
                ),
            ): bool,
        }
        if mode == MODE_EMBEDDED:
            fields.update(
                {
                    vol.Required(
                        CONF_TLS_PORT,
                        default=self.config_entry.data.get(
                            CONF_TLS_PORT, DEFAULT_TLS_PORT
                        ),
                    ): cv.port,
                    vol.Optional(
                        CONF_CERTIFICATE_NAME,
                        default=self.config_entry.data.get(
                            CONF_CERTIFICATE_NAME, ""
                        ),
                    ): str,
                    vol.Required(
                        CONF_CERTIFICATE_MODE,
                        default=self.config_entry.data.get(
                            CONF_CERTIFICATE_MODE, CERTIFICATE_AUTOMATIC
                        ),
                    ): vol.In((CERTIFICATE_AUTOMATIC, CERTIFICATE_CUSTOM)),
                    vol.Optional(
                        CONF_CERTIFICATE_PATH,
                        default=self.config_entry.data.get(
                            CONF_CERTIFICATE_PATH, ""
                        ),
                    ): str,
                    vol.Optional(
                        CONF_PRIVATE_KEY_PATH,
                        default=self.config_entry.data.get(
                            CONF_PRIVATE_KEY_PATH, ""
                        ),
                    ): str,
                    vol.Optional(
                        CONF_PRIVATE_KEY_PASSWORD,
                        default=self.config_entry.data.get(
                            CONF_PRIVATE_KEY_PASSWORD, ""
                        ),
                    ): str,
                }
            )
        else:
            fields.update(
                {
                    vol.Required(
                        CONF_HOST, default=self.config_entry.data.get(CONF_HOST, "")
                    ): str,
                    vol.Required(
                        CONF_PORT,
                        default=self.config_entry.data.get(CONF_PORT, DEFAULT_PORT),
                    ): cv.port,
                    vol.Required(
                        CONF_TOKEN,
                        default=self.config_entry.data.get(CONF_TOKEN, ""),
                    ): str,
                }
            )
        schema = vol.Schema(fields)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
