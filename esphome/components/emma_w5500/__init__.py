"""Second W5500 interface for Huawei EMMA isolated-network access."""

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome import pins
from esphome.components import esp32
from esphome.const import CONF_ID
from esphome.core import CORE

CONF_CLK_PIN = "clk_pin"
CONF_MOSI_PIN = "mosi_pin"
CONF_MISO_PIN = "miso_pin"
CONF_CS_PIN = "cs_pin"
CONF_INTERRUPT_PIN = "interrupt_pin"
CONF_MANUAL_IP = "manual_ip"
CONF_STATIC_IP = "static_ip"
CONF_GATEWAY = "gateway"
CONF_SUBNET = "subnet"

DEPENDENCIES = ["esp32"]
CODEOWNERS = ["@valexi7"]

emma_w5500_ns = cg.esphome_ns.namespace("emma_w5500")
EmmaW5500 = emma_w5500_ns.class_("EmmaW5500", cg.Component)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(EmmaW5500),
            cv.Required(CONF_CLK_PIN): pins.internal_gpio_output_pin_number,
            cv.Required(CONF_MOSI_PIN): pins.internal_gpio_output_pin_number,
            cv.Required(CONF_MISO_PIN): pins.internal_gpio_input_pin_number,
            cv.Required(CONF_CS_PIN): pins.internal_gpio_output_pin_number,
            cv.Required(CONF_INTERRUPT_PIN): pins.internal_gpio_input_pin_number,
            cv.Required(CONF_MANUAL_IP): cv.Schema(
                {
                    cv.Required(CONF_STATIC_IP): cv.ipv4address,
                    cv.Required(CONF_GATEWAY): cv.ipv4address,
                    cv.Required(CONF_SUBNET): cv.ipv4address,
                }
            ),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
)


async def to_code(config):
    if CORE.using_arduino:
        raise cv.Invalid("emma_w5500 requires the ESP-IDF framework")

    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(
        var.set_pins(
            config[CONF_CLK_PIN],
            config[CONF_MOSI_PIN],
            config[CONF_MISO_PIN],
            config[CONF_CS_PIN],
            config[CONF_INTERRUPT_PIN],
        )
    )
    manual_ip = config[CONF_MANUAL_IP]
    cg.add(
        var.set_manual_ip(
            str(manual_ip[CONF_STATIC_IP]),
            str(manual_ip[CONF_GATEWAY]),
            str(manual_ip[CONF_SUBNET]),
        )
    )

    esp32.include_builtin_idf_component("esp_eth")
    esp32.add_idf_sdkconfig_option("CONFIG_ETH_USE_SPI_ETHERNET", True)
    if esp32.idf_version() < cv.Version(6, 0, 0):
        esp32.add_idf_sdkconfig_option("CONFIG_ETH_SPI_ETHERNET_W5500", True)
    else:
        esp32.add_idf_component(name="espressif/w5500", ref="1.0.1")
