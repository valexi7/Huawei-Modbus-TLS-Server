"""Huawei EMMA reverse Modbus/TLS connector for ESPHome/ESP-IDF."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import binary_sensor, esp32, output, sensor, text_sensor
from esphome.const import (
    CONF_ID,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_TOTAL_INCREASING,
)
from esphome.core import CORE

from esphome.components.emma_w5500 import EmmaW5500
from .generated_contract import DEFAULT_API_PORT, DEFAULT_TLS_PORT

CONF_ETHERNET_ID = "ethernet_id"
CONF_TLS_PORT = "tls_port"
CONF_API_PORT = "api_port"
CONF_API_TOKEN = "api_token"
CONF_CERTIFICATE = "certificate"
CONF_PRIVATE_KEY = "private_key"
CONF_ACTIVITY_OUTPUT_ID = "activity_output_id"
CONF_CONNECTED = "connected"
CONF_DISCONNECTS = "disconnects"
CONF_LAST_ERROR = "last_error"
CONF_LOG_RAW = "log_raw"
CONF_READ_ONLY = "read_only"

DEPENDENCIES = ["esp32", "emma_w5500"]
AUTO_LOAD = ["binary_sensor", "sensor", "text_sensor"]
CODEOWNERS = ["@valexi7"]

# Connector API clients the ESP-IDF httpd keeps open at once (Home Assistant
# polls over one keep-alive connection; writes can briefly add more). Older
# ones are purged when the limit is reached.
HTTPD_MAX_OPEN_SOCKETS = 3

emma_reverse_ns = cg.esphome_ns.namespace("huawei_emma_reverse")
HuaweiEmmaReverse = emma_reverse_ns.class_("HuaweiEmmaReverse", cg.Component)


def _consume_sockets(config):
    """Reserve this component's sockets in ESPHome's lwIP socket budget.

    ESPHome sizes CONFIG_LWIP_MAX_SOCKETS from what components declare. This
    connector opens its own sockets outside ESPHome's socket layer, so without
    declaring them the pool stays at the default 10 and is exhausted once
    Home Assistant, EMMA and the API are connected. New connections are then
    accepted by lwIP and immediately dropped, which is how an OTA upload fails
    ("Device closed connection without responding").
    """
    try:
        from esphome.components import socket
    except ImportError:
        return config
    if not hasattr(socket, "consume_sockets") or not hasattr(socket, "SocketType"):
        return config
    name = "huawei_emma_reverse"
    # EMMA listener and connector API listener.
    socket.consume_sockets(2, name, socket.SocketType.TCP_LISTEN)(config)
    # One EMMA session plus the connector API clients.
    socket.consume_sockets(1 + HTTPD_MAX_OPEN_SOCKETS, name)(config)
    # ESPHome counts the OTA listener but not the upload connection itself.
    socket.consume_sockets(1, "ota_upload_headroom")(config)
    # esp_http_server's internal control socket.
    socket.consume_sockets(1, name, socket.SocketType.UDP)(config)
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(HuaweiEmmaReverse),
            cv.Required(CONF_ETHERNET_ID): cv.use_id(EmmaW5500),
            cv.Required(CONF_ACTIVITY_OUTPUT_ID): cv.use_id(output.BinaryOutput),
            cv.Optional(CONF_TLS_PORT, default=DEFAULT_TLS_PORT): cv.port,
            cv.Optional(CONF_API_PORT, default=DEFAULT_API_PORT): cv.port,
            cv.Required(CONF_API_TOKEN): cv.sensitive(cv.string_strict),
            cv.Required(CONF_CERTIFICATE): cv.sensitive(cv.string_strict),
            cv.Required(CONF_PRIVATE_KEY): cv.sensitive(cv.string_strict),
            cv.Optional(CONF_LOG_RAW, default=False): cv.boolean,
            # EMMA read-only mode for installations controlled by an external
            # reserve market provider: telemetry only, every write rejected.
            cv.Optional(CONF_READ_ONLY, default=False): cv.boolean,
            cv.Optional(
                CONF_CONNECTED,
                default={"name": "EMMA TLS Connected", "device_class": "connectivity"},
            ): binary_sensor.binary_sensor_schema(),
            # Number of times the EMMA TLS session dropped since this boot.
            # Together with the board package's Uptime/Reset Reason it tells
            # an EMMA-side disconnect apart from an ESP32 reboot.
            cv.Optional(
                CONF_DISCONNECTS, default={"name": "EMMA TLS Disconnects"}
            ): sensor.sensor_schema(
                icon="mdi:lan-disconnect",
                accuracy_decimals=0,
                state_class=STATE_CLASS_TOTAL_INCREASING,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
            cv.Optional(
                CONF_LAST_ERROR, default={"name": "EMMA Last Error"}
            ): text_sensor.text_sensor_schema(
                icon="mdi:alert-circle-outline",
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
    _consume_sockets,
)



async def to_code(config):
    if CORE.using_arduino:
        raise cv.Invalid("huawei_emma_reverse requires the ESP-IDF framework")

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    ethernet = await cg.get_variable(config[CONF_ETHERNET_ID])
    activity_output = await cg.get_variable(config[CONF_ACTIVITY_OUTPUT_ID])
    connected = await binary_sensor.new_binary_sensor(config[CONF_CONNECTED])
    cg.add(var.set_ethernet(ethernet))
    cg.add(var.set_activity_output(activity_output))
    cg.add(var.set_connected_sensor(connected))
    disconnects = await sensor.new_sensor(config[CONF_DISCONNECTS])
    cg.add(var.set_disconnects_sensor(disconnects))
    last_error = await text_sensor.new_text_sensor(config[CONF_LAST_ERROR])
    cg.add(var.set_last_error_sensor(last_error))
    cg.add(var.set_ports(config[CONF_TLS_PORT], config[CONF_API_PORT]))
    cg.add_define("HUAWEI_EMMA_HTTPD_MAX_OPEN_SOCKETS", HTTPD_MAX_OPEN_SOCKETS)
    cg.add(var.set_api_token(config[CONF_API_TOKEN]))
    cg.add(var.set_tls_material(config[CONF_CERTIFICATE], config[CONF_PRIVATE_KEY]))
    cg.add(var.set_log_raw(config[CONF_LOG_RAW]))
    cg.add(var.set_read_only(config[CONF_READ_ONLY]))

    # ESPHome 2026.11+ compiles the mbedTLS TLS stack out unless a component
    # asks for it; this connector uses mbedtls_ssl_* directly.
    if hasattr(esp32, "require_mbedtls_tls"):
        esp32.require_mbedtls_tls()
    esp32.include_builtin_idf_component("esp_http_server")
    esp32.include_builtin_idf_component("json")
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_SSL_PROTO_TLS1_2", True)
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_PEM_PARSE_C", True)
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_X509_CRT_PARSE_C", True)
