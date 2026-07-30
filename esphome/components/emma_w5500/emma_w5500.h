#pragma once

#include "esphome/core/component.h"

#include <esp_eth.h>
#include <esp_event.h>
#include <esp_netif.h>

#include <string>

namespace esphome::emma_w5500 {

class EmmaW5500 : public Component {
 public:
  void set_pins(int clk, int mosi, int miso, int cs, int interrupt) {
    this->clk_pin_ = clk;
    this->mosi_pin_ = mosi;
    this->miso_pin_ = miso;
    this->cs_pin_ = cs;
    this->interrupt_pin_ = interrupt;
  }
  void set_manual_ip(const std::string &ip, const std::string &gateway, const std::string &subnet) {
    this->ip_ = ip;
    this->gateway_ = gateway;
    this->subnet_ = subnet;
  }

  void setup() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::WIFI + 1.0f; }

  bool is_link_up() const { return this->link_up_; }
  bool has_ip() const { return this->has_ip_; }
  esp_netif_t *get_netif() const { return this->netif_; }
  const std::string &get_ip_address() const { return this->ip_; }

 protected:
  static void eth_event_(void *arg, esp_event_base_t base, int32_t id, void *data);
  static void ip_event_(void *arg, esp_event_base_t base, int32_t id, void *data);
  bool check_(esp_err_t error, const char *operation);

  int clk_pin_{-1};
  int mosi_pin_{-1};
  int miso_pin_{-1};
  int cs_pin_{-1};
  int interrupt_pin_{-1};
  std::string ip_;
  std::string gateway_;
  std::string subnet_;
  esp_eth_handle_t eth_handle_{nullptr};
  esp_netif_t *netif_{nullptr};
  esp_event_handler_instance_t eth_event_instance_{nullptr};
  esp_event_handler_instance_t ip_event_instance_{nullptr};
  bool link_up_{false};
  bool has_ip_{false};
};

}  // namespace esphome::emma_w5500
