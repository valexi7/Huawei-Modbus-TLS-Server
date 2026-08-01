"""Huawei EMMA reverse Modbus/TLS connector for ESPHome/ESP-IDF."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import binary_sensor, esp32, output
from esphome.const import CONF_ID
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
CONF_LOG_RAW = "log_raw"

DEPENDENCIES = ["esp32", "emma_w5500"]
AUTO_LOAD = ["binary_sensor"]
CODEOWNERS = ["@valexi7"]

emma_reverse_ns = cg.esphome_ns.namespace("huawei_emma_reverse")
HuaweiEmmaReverse = emma_reverse_ns.class_("HuaweiEmmaReverse", cg.Component)

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
            cv.Optional(
                CONF_CONNECTED,
                default={"name": "EMMA TLS Connected", "device_class": "connectivity"},
            ): binary_sensor.binary_sensor_schema(),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
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
    cg.add(var.set_ports(config[CONF_TLS_PORT], config[CONF_API_PORT]))
    cg.add(var.set_api_token(config[CONF_API_TOKEN]))
    cg.add(var.set_tls_material(config[CONF_CERTIFICATE], config[CONF_PRIVATE_KEY]))
    cg.add(var.set_log_raw(config[CONF_LOG_RAW]))

    esp32.include_builtin_idf_component("esp_http_server")
    esp32.include_builtin_idf_component("json")
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_SSL_PROTO_TLS1_2", True)
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_PEM_PARSE_C", True)
    esp32.add_idf_sdkconfig_option("CONFIG_MBEDTLS_X509_CRT_PARSE_C", True)
