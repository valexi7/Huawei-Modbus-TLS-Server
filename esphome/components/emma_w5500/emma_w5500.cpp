#include "emma_w5500.h"

#include "esphome/core/log.h"

#include <driver/gpio.h>
#include <driver/spi_master.h>
#include <esp_idf_version.h>
#include <esp_mac.h>

#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(6, 0, 0)
#include <esp_eth_mac_w5500.h>
#include <esp_eth_phy_w5500.h>
#endif

namespace esphome::emma_w5500 {

static const char *const TAG = "emma_w5500";

bool EmmaW5500::check_(esp_err_t error, const char *operation) {
  if (error == ESP_OK)
    return true;
  ESP_LOGE(TAG, "%s failed: %s", operation, esp_err_to_name(error));
  this->mark_failed();
  return false;
}

void EmmaW5500::setup() {
  ESP_LOGI(TAG, "Initializing isolated EMMA W5500 interface");

  esp_err_t error = gpio_install_isr_service(0);
  if (error != ESP_OK && error != ESP_ERR_INVALID_STATE) {
    this->check_(error, "GPIO ISR service");
    return;
  }

  spi_bus_config_t bus_config{};
  bus_config.mosi_io_num = this->mosi_pin_;
  bus_config.miso_io_num = this->miso_pin_;
  bus_config.sclk_io_num = this->clk_pin_;
  bus_config.quadwp_io_num = -1;
  bus_config.quadhd_io_num = -1;
  if (!this->check_(spi_bus_initialize(SPI3_HOST, &bus_config, SPI_DMA_CH_AUTO), "SPI3 initialization"))
    return;

  esp_netif_config_t netif_config = ESP_NETIF_DEFAULT_ETH();
  this->netif_ = esp_netif_new(&netif_config);
  if (this->netif_ == nullptr) {
    ESP_LOGE(TAG, "Failed to create EMMA Ethernet netif");
    this->mark_failed();
    return;
  }

  spi_device_interface_config_t device_config{};
  device_config.mode = 0;
  device_config.clock_speed_hz = 26 * 1000 * 1000;
  device_config.spics_io_num = this->cs_pin_;
  device_config.queue_size = 20;

  eth_w5500_config_t w5500_config = ETH_W5500_DEFAULT_CONFIG(SPI3_HOST, &device_config);
  w5500_config.int_gpio_num = this->interrupt_pin_;
  eth_mac_config_t mac_config = ETH_MAC_DEFAULT_CONFIG();
  eth_phy_config_t phy_config = ETH_PHY_DEFAULT_CONFIG();
  phy_config.phy_addr = 1;
  phy_config.reset_gpio_num = -1;

  esp_eth_mac_t *mac = esp_eth_mac_new_w5500(&w5500_config, &mac_config);
  esp_eth_phy_t *phy = esp_eth_phy_new_w5500(&phy_config);
  if (mac == nullptr || phy == nullptr) {
    ESP_LOGE(TAG, "Failed to allocate W5500 MAC/PHY");
    this->mark_failed();
    return;
  }

  esp_eth_config_t eth_config = ETH_DEFAULT_CONFIG(mac, phy);
  if (!this->check_(esp_eth_driver_install(&eth_config, &this->eth_handle_), "Ethernet driver install"))
    return;

  uint8_t mac_address[6];
  if (!this->check_(esp_read_mac(mac_address, ESP_MAC_ETH), "Ethernet MAC read"))
    return;
  if (!this->check_(esp_eth_ioctl(this->eth_handle_, ETH_CMD_S_MAC_ADDR, mac_address), "Ethernet MAC set"))
    return;

  esp_eth_netif_glue_handle_t glue = esp_eth_new_netif_glue(this->eth_handle_);
  if (!this->check_(esp_netif_attach(this->netif_, glue), "Ethernet netif attach"))
    return;

  esp_netif_dhcpc_stop(this->netif_);
  esp_netif_ip_info_t ip_info{};
  if (esp_netif_str_to_ip4(this->ip_.c_str(), &ip_info.ip) != ESP_OK ||
      esp_netif_str_to_ip4(this->gateway_.c_str(), &ip_info.gw) != ESP_OK ||
      esp_netif_str_to_ip4(this->subnet_.c_str(), &ip_info.netmask) != ESP_OK) {
    ESP_LOGE(TAG, "Invalid static IPv4 configuration");
    this->mark_failed();
    return;
  }
  if (!this->check_(esp_netif_set_ip_info(this->netif_, &ip_info), "Static IPv4 setup"))
    return;

  if (!this->check_(esp_event_handler_instance_register(ETH_EVENT, ESP_EVENT_ANY_ID, &EmmaW5500::eth_event_, this,
                                                         &this->eth_event_instance_),
                    "Ethernet event registration"))
    return;
  if (!this->check_(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, &EmmaW5500::ip_event_, this,
                                                         &this->ip_event_instance_),
                    "IP event registration"))
    return;
  this->check_(esp_eth_start(this->eth_handle_), "Ethernet start");
}

void EmmaW5500::eth_event_(void *arg, esp_event_base_t, int32_t id, void *data) {
  auto *self = static_cast<EmmaW5500 *>(arg);
  auto *handle = static_cast<esp_eth_handle_t *>(data);
  if (handle != nullptr && *handle != self->eth_handle_)
    return;
  if (id == ETHERNET_EVENT_CONNECTED) {
    self->link_up_ = true;
    ESP_LOGI(TAG, "EMMA Ethernet link is up");
  } else if (id == ETHERNET_EVENT_DISCONNECTED) {
    self->link_up_ = false;
    self->has_ip_ = false;
    ESP_LOGW(TAG, "EMMA Ethernet link is down");
  }
}

void EmmaW5500::ip_event_(void *arg, esp_event_base_t, int32_t id, void *data) {
  auto *self = static_cast<EmmaW5500 *>(arg);
  if (id != IP_EVENT_ETH_GOT_IP)
    return;
  auto *event = static_cast<ip_event_got_ip_t *>(data);
  if (event == nullptr || event->esp_netif != self->netif_)
    return;
  self->has_ip_ = true;
  ESP_LOGI(TAG, "EMMA Ethernet ready at " IPSTR, IP2STR(&event->ip_info.ip));
}

void EmmaW5500::dump_config() {
  ESP_LOGCONFIG(TAG, "Huawei EMMA isolated W5500:");
  ESP_LOGCONFIG(TAG, "  Address: %s/%s", this->ip_.c_str(), this->subnet_.c_str());
  ESP_LOGCONFIG(TAG, "  Gateway: %s", this->gateway_.c_str());
  ESP_LOGCONFIG(TAG, "  Pins: SCLK=%d MOSI=%d MISO=%d CS=%d INT=%d", this->clk_pin_, this->mosi_pin_,
                this->miso_pin_, this->cs_pin_, this->interrupt_pin_);
}

}  // namespace esphome::emma_w5500
