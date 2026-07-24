from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import ClientError, ClientSession

from .embedded_server import RuntimeState


_LOGGER = logging.getLogger(__name__)


class EmmaApiError(RuntimeError):
    pass


class EmmaApiAuthError(EmmaApiError):
    pass


class EmmaApiConnectionError(EmmaApiError):
    pass


class EmmaApiClient:
    def __init__(self, session: ClientSession, host: str, port: int, token: str) -> None:
        self._session = session
        self._base_url = f"http://{host}:{port}/api/v1"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers,
                json=payload,
                timeout=10,
            ) as response:
                body = await response.text()
                try:
                    value = json.loads(body)
                except json.JSONDecodeError as error:
                    raise EmmaApiError(
                        f"Connector returned HTTP {response.status} with invalid JSON"
                    ) from error
                if not isinstance(value, dict):
                    raise EmmaApiError("Connector returned a non-object JSON response")
                if response.status >= 400:
                    detail = value.get("error") or value.get("message")
                    message = f"Connector returned HTTP {response.status}"
                    if detail:
                        message = f"{message}: {detail}"
                    if response.status == 401:
                        raise EmmaApiAuthError(message)
                    raise EmmaApiError(message)
                return value
        except (ClientError, TimeoutError) as error:
            raise EmmaApiConnectionError(
                f"Cannot communicate with EMMA connector: {error}"
            ) from error

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def device(self) -> dict[str, Any]:
        return await self._request("GET", "/device")

    async def entities(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/entities")
        return payload.get("entities", [])

    async def states(self) -> dict[str, Any]:
        return await self._request("GET", "/states")

    async def set_value(self, register_name: str, value: Any) -> Any:
        _LOGGER.debug(
            "Connector command POST register=%s value=%r", register_name, value
        )
        payload = await self._request(
            "POST", f"/entities/{register_name}/value", {"value": value}
        )
        result = payload.get("value")
        _LOGGER.debug(
            "Connector command response register=%s value=%r", register_name, result
        )
        return result

    async def set_tou_periods(self, periods: list[dict[str, Any]]) -> Any:
        _LOGGER.debug("Connector TOU command POST periods=%r", periods)
        payload = await self._request("POST", "/tou-periods", {"periods": periods})
        result = payload.get("value")
        _LOGGER.debug("Connector TOU command response value=%r", result)
        return result


class EmbeddedEmmaApiClient:
    """In-process adapter using the same contract as the external HTTP API."""

    def __init__(self, state: RuntimeState) -> None:
        self._state = state

    async def health(self) -> dict[str, Any]:
        return self._state.health()

    async def device(self) -> dict[str, Any]:
        return self._state.device()

    async def entities(self) -> list[dict[str, Any]]:
        return self._state.entities()

    async def states(self) -> dict[str, Any]:
        return self._state.states()

    async def set_value(self, register_name: str, value: Any) -> Any:
        _LOGGER.debug(
            "Embedded command register=%s value=%r", register_name, value
        )
        try:
            result = await self._state.set_value(register_name, value)
        except ValueError as error:
            raise EmmaApiError(str(error)) from error
        except (ConnectionError, TimeoutError) as error:
            raise EmmaApiConnectionError(str(error)) from error
        except Exception as error:
            raise EmmaApiError(str(error)) from error
        _LOGGER.debug(
            "Embedded command response register=%s value=%r", register_name, result
        )
        return result

    async def set_tou_periods(self, periods: list[dict[str, Any]]) -> Any:
        _LOGGER.debug("Embedded TOU command periods=%r", periods)
        try:
            result = await self._state.set_tou_periods(periods)
        except ValueError as error:
            raise EmmaApiError(str(error)) from error
        except (ConnectionError, TimeoutError) as error:
            raise EmmaApiConnectionError(str(error)) from error
        except Exception as error:
            raise EmmaApiError(str(error)) from error
        _LOGGER.debug("Embedded TOU command response value=%r", result)
        return result
