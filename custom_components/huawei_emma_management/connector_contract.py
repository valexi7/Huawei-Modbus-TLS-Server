"""Dependency-free connector protocol and runtime defaults.

This module is the single configuration path shared by the Home Assistant
embedded server, the standalone Python server, and generated ESP32 firmware.
Keep deployment-specific addresses, credentials, and TLS material outside it.
"""

from __future__ import annotations


CONTRACT_VERSION = 2
API_PREFIX = "/api/v1"
DEFAULT_API_PORT = 8088
DEFAULT_TLS_PORT = 16100
DEFAULT_REQUEST_TIMEOUT_SECONDS = 5
# Large enough for a subscription containing the complete generated catalog. The
# ESP32 allocates it only while processing a request; catalog responses are streamed.
MAX_HTTP_REQUEST_BODY = 32768
MAX_MBAP_LENGTH = 512
MAX_READ_REGISTERS = 125
MAX_WRITE_REGISTERS = 123
TOU_MAX_PERIODS = 14

POLL_INTERVALS = {
    "fast": 30,
    "medium": 300,
    "slow": 1800,
}

API_PATHS = {
    "health": "/health",
    "device": "/device",
    "entities": "/entities",
    "states": "/states",
    "subscriptions": "/subscriptions",
    "tou_periods": "/tou-periods",
    "entity_value_template": "/entities/{register_name}/value",
}
