#include "huawei_emma_reverse.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <cJSON.h>
#include <esp_idf_version.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <sstream>

namespace esphome::huawei_emma_reverse {

static const char *const TAG = "huawei_emma_reverse";
void HuaweiEmmaReverse::setup() {
  ESP_LOGI(TAG, "Starting ESP32 Huawei EMMA reverse connector");
  this->tls_mutex_ = xSemaphoreCreateMutex();
  this->data_mutex_ = xSemaphoreCreateMutex();
  if (this->tls_mutex_ == nullptr || this->data_mutex_ == nullptr) {
    ESP_LOGE(TAG, "Failed to allocate connector mutexes");
    this->mark_failed();
    return;
  }
  if (this->certificate_.find("BEGIN CERTIFICATE") == std::string::npos ||
      this->private_key_.find("BEGIN") == std::string::npos) {
    ESP_LOGE(TAG, "TLS certificate/private key are not PEM values");
    this->mark_failed();
    return;
  }
  if (this->api_token_.size() < 24) {
    ESP_LOGE(TAG, "api_token must contain at least 24 characters");
    this->mark_failed();
    return;
  }
  this->activity_output_->turn_off();
  this->connected_sensor_->publish_initial_state(false);
  this->initialize_subscriptions_();
  if (!this->start_http_server_()) {
    this->mark_failed();
    return;
  }
  if (xTaskCreatePinnedToCore(&HuaweiEmmaReverse::tls_task_entry_, "emma_tls", 24576, this, 5,
                              &this->tls_task_handle_, 1) != pdPASS) {
    ESP_LOGE(TAG, "Failed to start EMMA TLS task");
    httpd_stop(this->http_server_);
    this->http_server_ = nullptr;
    this->mark_failed();
  }
}

void HuaweiEmmaReverse::loop() { this->update_activity_led_(); }

void HuaweiEmmaReverse::dump_config() {
  ESP_LOGCONFIG(TAG, "Huawei EMMA reverse Modbus/TLS connector:");
  ESP_LOGCONFIG(TAG, "  EMMA interface: %s:%u", this->ethernet_->get_ip_address().c_str(), this->tls_port_);
  ESP_LOGCONFIG(TAG, "  Home Assistant connector API port: %u", this->api_port_);
  ESP_LOGCONFIG(TAG, "  Raw Modbus logging: %s", YESNO(this->log_raw_));
  ESP_LOGCONFIG(TAG, "  TLS certificate: embedded (%u bytes)", static_cast<unsigned>(this->certificate_.size()));
  ESP_LOGCONFIG(TAG, "  Activity LED: GPIO binary; timing-coded (RGB is not available on T-ETH-Elite)");
  ESP_LOGCONFIG(TAG, "  Connector contract: v%u; generated catalog: %u registers", GENERATED_CONTRACT_VERSION,
                static_cast<unsigned>(GENERATED_ENTITY_COUNT));
}

void HuaweiEmmaReverse::tls_task_entry_(void *parameter) {
  static_cast<HuaweiEmmaReverse *>(parameter)->tls_task_();
  vTaskDelete(nullptr);
}

bool HuaweiEmmaReverse::configure_tls_() {
  mbedtls_net_init(&this->listen_fd_);
  mbedtls_net_init(&this->client_fd_);
  mbedtls_ssl_init(&this->ssl_);
  mbedtls_ssl_config_init(&this->ssl_config_);
  mbedtls_x509_crt_init(&this->certificate_chain_);
  mbedtls_pk_init(&this->private_key_context_);
  mbedtls_entropy_init(&this->entropy_);
  mbedtls_ctr_drbg_init(&this->random_);

  const char *personalization = "huawei_emma_reverse";
  int error = mbedtls_ctr_drbg_seed(&this->random_, mbedtls_entropy_func, &this->entropy_,
                                    reinterpret_cast<const unsigned char *>(personalization),
                                    std::strlen(personalization));
  if (error != 0) {
    ESP_LOGE(TAG, "TLS random initialization failed: -0x%04X", -error);
    return false;
  }
  error = mbedtls_x509_crt_parse(&this->certificate_chain_,
                                 reinterpret_cast<const unsigned char *>(this->certificate_.c_str()),
                                 this->certificate_.size() + 1);
  if (error < 0) {
    ESP_LOGE(TAG, "TLS certificate parsing failed: -0x%04X", -error);
    return false;
  }
#if MBEDTLS_VERSION_NUMBER >= 0x03000000
  error = mbedtls_pk_parse_key(&this->private_key_context_,
                               reinterpret_cast<const unsigned char *>(this->private_key_.c_str()),
                               this->private_key_.size() + 1, nullptr, 0, mbedtls_ctr_drbg_random, &this->random_);
#else
  error = mbedtls_pk_parse_key(&this->private_key_context_,
                               reinterpret_cast<const unsigned char *>(this->private_key_.c_str()),
                               this->private_key_.size() + 1, nullptr, 0);
#endif
  if (error != 0) {
    ESP_LOGE(TAG, "TLS private-key parsing failed: -0x%04X", -error);
    return false;
  }
  error = mbedtls_ssl_config_defaults(&this->ssl_config_, MBEDTLS_SSL_IS_SERVER, MBEDTLS_SSL_TRANSPORT_STREAM,
                                      MBEDTLS_SSL_PRESET_DEFAULT);
  if (error != 0)
    return false;
  mbedtls_ssl_conf_rng(&this->ssl_config_, mbedtls_ctr_drbg_random, &this->random_);
  mbedtls_ssl_conf_authmode(&this->ssl_config_, MBEDTLS_SSL_VERIFY_NONE);
  mbedtls_ssl_conf_read_timeout(&this->ssl_config_, 5000);
  if (mbedtls_ssl_conf_own_cert(&this->ssl_config_, &this->certificate_chain_, &this->private_key_context_) != 0)
    return false;
  if (mbedtls_ssl_setup(&this->ssl_, &this->ssl_config_) != 0)
    return false;

  char port[8];
  std::snprintf(port, sizeof(port), "%u", this->tls_port_);
  error = mbedtls_net_bind(&this->listen_fd_, this->ethernet_->get_ip_address().c_str(), port, MBEDTLS_NET_PROTO_TCP);
  if (error != 0) {
    ESP_LOGE(TAG, "Cannot bind TLS listener to %s:%s: -0x%04X", this->ethernet_->get_ip_address().c_str(), port,
             -error);
    return false;
  }
  this->tls_ready_ = true;
  ESP_LOGI(TAG, "Huawei reverse Modbus/TLS listening on %s:%s", this->ethernet_->get_ip_address().c_str(), port);
  return true;
}

bool HuaweiEmmaReverse::accept_tls_client_() {
  mbedtls_net_free(&this->client_fd_);
  mbedtls_net_init(&this->client_fd_);
  int error = mbedtls_net_accept(&this->listen_fd_, &this->client_fd_, nullptr, 0, nullptr);
  if (error != 0)
    return false;
  mbedtls_ssl_session_reset(&this->ssl_);
  mbedtls_ssl_set_bio(&this->ssl_, &this->client_fd_, mbedtls_net_send, mbedtls_net_recv, mbedtls_net_recv_timeout);
  do {
    error = mbedtls_ssl_handshake(&this->ssl_);
  } while (error == MBEDTLS_ERR_SSL_WANT_READ || error == MBEDTLS_ERR_SSL_WANT_WRITE);
  if (error != 0) {
    ESP_LOGW(TAG, "EMMA TLS handshake failed: -0x%04X", -error);
    this->close_tls_client_();
    return false;
  }
  this->set_connected_(true);
  this->connected_at_ms_ = millis();
  this->reconnect_count_++;
  ESP_LOGI(TAG, "EMMA TLS client connected; ESP32 is now Modbus master");
  return true;
}

void HuaweiEmmaReverse::close_tls_client_() {
  if (this->connected_.load())
    mbedtls_ssl_close_notify(&this->ssl_);
  mbedtls_net_free(&this->client_fd_);
  this->set_connected_(false);
}

void HuaweiEmmaReverse::tls_task_() {
  while (!this->ethernet_->has_ip())
    vTaskDelay(pdMS_TO_TICKS(250));
  if (!this->configure_tls_()) {
    this->set_last_error_("TLS initialization failed");
    return;
  }
  for (;;) {
    if (!this->accept_tls_client_()) {
      vTaskDelay(pdMS_TO_TICKS(1000));
      continue;
    }

    ModbusFrame startup;
    xSemaphoreTake(this->tls_mutex_, portMAX_DELAY);
    const bool startup_ok = this->read_frame_(startup);
    xSemaphoreGive(this->tls_mutex_);
    if (startup_ok)
      this->handle_unsolicited_(startup);
    this->discover_topology_();
    this->poll_group_("slow");
    this->poll_group_("medium");
    this->poll_group_("fast");
    uint32_t next_fast = millis() + GENERATED_FAST_POLL_MS;
    uint32_t next_medium = millis() + GENERATED_MEDIUM_POLL_MS;
    uint32_t next_slow = millis() + GENERATED_SLOW_POLL_MS;

    while (this->connected_.load()) {
      const uint32_t now = millis();
      if (this->subscriptions_changed_.exchange(false)) {
        this->poll_group_("slow");
        this->poll_group_("medium");
        this->poll_group_("fast");
        next_fast = now + GENERATED_FAST_POLL_MS;
        next_medium = now + GENERATED_MEDIUM_POLL_MS;
        next_slow = now + GENERATED_SLOW_POLL_MS;
      }
      if (static_cast<int32_t>(now - next_fast) >= 0) {
        this->poll_group_("fast");
        next_fast = now + GENERATED_FAST_POLL_MS;
      }
      if (static_cast<int32_t>(now - next_medium) >= 0) {
        this->poll_group_("medium");
        next_medium = now + GENERATED_MEDIUM_POLL_MS;
      }
      if (static_cast<int32_t>(now - next_slow) >= 0) {
        this->poll_group_("slow");
        next_slow = now + GENERATED_SLOW_POLL_MS;
      }
      vTaskDelay(pdMS_TO_TICKS(100));
    }
    this->close_tls_client_();
    vTaskDelay(pdMS_TO_TICKS(500));
  }
}

bool HuaweiEmmaReverse::read_exact_(uint8_t *data, size_t length) {
  size_t offset = 0;
  while (offset < length) {
    int result = mbedtls_ssl_read(&this->ssl_, data + offset, length - offset);
    if (result > 0) {
      offset += result;
      continue;
    }
    if (result == MBEDTLS_ERR_SSL_WANT_READ || result == MBEDTLS_ERR_SSL_WANT_WRITE)
      continue;
    this->set_last_error_("TLS read failed");
    this->set_connected_(false);
    return false;
  }
  return true;
}

bool HuaweiEmmaReverse::write_all_(const uint8_t *data, size_t length) {
  size_t offset = 0;
  while (offset < length) {
    int result = mbedtls_ssl_write(&this->ssl_, data + offset, length - offset);
    if (result > 0) {
      offset += result;
      continue;
    }
    if (result == MBEDTLS_ERR_SSL_WANT_READ || result == MBEDTLS_ERR_SSL_WANT_WRITE)
      continue;
    this->set_last_error_("TLS write failed");
    this->set_connected_(false);
    return false;
  }
  return true;
}

bool HuaweiEmmaReverse::read_frame_(ModbusFrame &frame) {
  uint8_t header[6];
  if (!this->read_exact_(header, sizeof(header)))
    return false;
  frame.transaction = static_cast<uint16_t>(header[0] << 8 | header[1]);
  frame.protocol = static_cast<uint16_t>(header[2] << 8 | header[3]);
  uint16_t length = static_cast<uint16_t>(header[4] << 8 | header[5]);
  if (length < 2 || length > GENERATED_MAX_MBAP_LENGTH) {
    ESP_LOGW(TAG, "Rejected MBAP length %u", length);
    this->set_connected_(false);
    return false;
  }
  std::vector<uint8_t> body(length);
  if (!this->read_exact_(body.data(), body.size()))
    return false;
  frame.unit = body[0];
  frame.pdu.assign(body.begin() + 1, body.end());
  this->signal_activity_(ActivityKind::MODBUS_RX);
  if (this->log_raw_) {
    ESP_LOGD(TAG, "RX transaction=%u unit=%u fc=0x%02X bytes=%u", frame.transaction, frame.unit,
             frame.pdu.empty() ? 0 : frame.pdu[0], static_cast<unsigned>(length + 6));
  }
  return true;
}

bool HuaweiEmmaReverse::request_(uint8_t unit, const std::vector<uint8_t> &pdu, ModbusFrame &response) {
  if (!this->connected_.load())
    return false;
  const uint16_t transaction = this->next_transaction_++;
  const uint16_t length = static_cast<uint16_t>(pdu.size() + 1);
  std::vector<uint8_t> frame(7 + pdu.size());
  frame[0] = transaction >> 8;
  frame[1] = transaction & 0xFF;
  frame[4] = length >> 8;
  frame[5] = length & 0xFF;
  frame[6] = unit;
  std::copy(pdu.begin(), pdu.end(), frame.begin() + 7);
  this->signal_activity_(ActivityKind::MODBUS_TX);
  if (this->log_raw_)
    ESP_LOGD(TAG, "TX transaction=%u unit=%u fc=0x%02X bytes=%u", transaction, unit, pdu[0],
             static_cast<unsigned>(frame.size()));
  if (!this->write_all_(frame.data(), frame.size()))
    return false;
  for (;;) {
    if (!this->read_frame_(response))
      return false;
    if (response.transaction == transaction)
      break;
    this->handle_unsolicited_(response);
  }
  if (response.protocol != 0 || response.pdu.empty() || (response.pdu[0] & 0x80) != 0) {
    ESP_LOGW(TAG, "Modbus request failed transaction=%u fc=0x%02X exception=%u", transaction,
             response.pdu.empty() ? 0 : response.pdu[0], response.pdu.size() > 1 ? response.pdu[1] : 0);
    return false;
  }
  return true;
}

bool HuaweiEmmaReverse::read_registers_(uint8_t unit, uint16_t address, uint16_t count,
                                        std::vector<uint16_t> &values) {
  if (count == 0 || count > GENERATED_MAX_READ_REGISTERS)
    return false;
  std::vector<uint8_t> pdu{0x03, static_cast<uint8_t>(address >> 8), static_cast<uint8_t>(address),
                           static_cast<uint8_t>(count >> 8), static_cast<uint8_t>(count)};
  ModbusFrame response;
  if (!this->request_(unit, pdu, response) || response.pdu.size() != 2 + count * 2 ||
      response.pdu[0] != 0x03 || response.pdu[1] != count * 2)
    return false;
  values.resize(count);
  for (size_t index = 0; index < count; index++)
    values[index] = static_cast<uint16_t>(response.pdu[2 + index * 2] << 8 | response.pdu[3 + index * 2]);
  return true;
}

bool HuaweiEmmaReverse::write_registers_(uint8_t unit, uint16_t address, const std::vector<uint16_t> &values) {
  if (values.empty() || values.size() > GENERATED_MAX_WRITE_REGISTERS)
    return false;
  std::vector<uint8_t> pdu{0x10, static_cast<uint8_t>(address >> 8), static_cast<uint8_t>(address),
                           static_cast<uint8_t>(values.size() >> 8), static_cast<uint8_t>(values.size()),
                           static_cast<uint8_t>(values.size() * 2)};
  for (uint16_t value : values) {
    pdu.push_back(value >> 8);
    pdu.push_back(value & 0xFF);
  }
  ModbusFrame response;
  return this->request_(unit, pdu, response) && response.pdu.size() == 5 && response.pdu[0] == 0x10;
}

bool HuaweiEmmaReverse::write_single_register_(uint8_t unit, uint16_t address, uint16_t value) {
  std::vector<uint8_t> pdu{0x06, static_cast<uint8_t>(address >> 8), static_cast<uint8_t>(address),
                           static_cast<uint8_t>(value >> 8), static_cast<uint8_t>(value)};
  ModbusFrame response;
  return this->request_(unit, pdu, response) && response.pdu == pdu;
}

void HuaweiEmmaReverse::handle_unsolicited_(const ModbusFrame &frame) {
  if (frame.pdu.empty() || frame.pdu[0] != 0x41)
    return;
  TopologyDevice startup;
  this->parse_device_info_(frame.pdu.data(), frame.pdu.size(), startup);
  if (!startup.model.empty()) {
    startup.role = "emma";
    startup.unit_id = 0;
    xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
    this->emma_ = startup;
    xSemaphoreGive(this->data_mutex_);
    ESP_LOGI(TAG, "Huawei startup model=%s firmware=%s serial=%s protocol=%s", startup.model.c_str(),
             startup.firmware.c_str(), startup.serial.c_str(), startup.protocol.c_str());
  }
}

void HuaweiEmmaReverse::parse_device_info_(const uint8_t *data, size_t length, TopologyDevice &device) {
  const char marker[] = "1=";
  const uint8_t *start = nullptr;
  for (size_t index = 0; index + 2 < length; index++) {
    if (data[index] == marker[0] && data[index + 1] == marker[1]) {
      start = data + index;
      break;
    }
  }
  if (start == nullptr)
    return;
  std::string text(reinterpret_cast<const char *>(start), length - static_cast<size_t>(start - data));
  const size_t terminator = text.find('\0');
  if (terminator != std::string::npos)
    text.resize(terminator);
  size_t offset = 0;
  while (offset < text.size()) {
    size_t end = text.find(';', offset);
    std::string item = text.substr(offset, end == std::string::npos ? std::string::npos : end - offset);
    size_t equal = item.find('=');
    if (equal != std::string::npos) {
      const std::string key = item.substr(0, equal);
      const std::string value = item.substr(equal + 1);
      if (key == "1") device.model = value;
      else if (key == "2") device.firmware = value;
      else if (key == "3") device.protocol = value;
      else if (key == "4") device.serial = value;
      else if (key == "5") device.unit_id = static_cast<uint8_t>(std::atoi(value.c_str()));
      else if (key == "8") device.product_type = value;
    }
    if (end == std::string::npos)
      break;
    offset = end + 1;
  }
  device.role = role_for_(device.model, device.product_type);
}

void HuaweiEmmaReverse::discover_topology_() {
  std::vector<TopologyDevice> devices;
  uint8_t object = 0x87;
  for (size_t page_index = 0; page_index < 32; page_index++) {
    std::vector<uint8_t> pdu{0x2B, 0x0E, 0x03, object};
    ModbusFrame response;
    xSemaphoreTake(this->tls_mutex_, portMAX_DELAY);
    bool ok = this->request_(0, pdu, response);
    xSemaphoreGive(this->tls_mutex_);
    if (!ok || response.pdu.size() < 7 || response.pdu[0] != 0x2B)
      break;
    const bool more = response.pdu[4] != 0;
    const uint8_t next = response.pdu[5];
    size_t offset = 7;
    while (offset + 2 <= response.pdu.size()) {
      uint8_t id = response.pdu[offset++];
      uint8_t size = response.pdu[offset++];
      if (offset + size > response.pdu.size())
        break;
      if (id != 0x87) {
        TopologyDevice device;
        device.object_id = id;
        this->parse_device_info_(response.pdu.data() + offset, size, device);
        if (!device.model.empty())
          devices.push_back(device);
      }
      offset += size;
    }
    if (!more)
      break;
    object = next;
  }
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  this->topology_ = devices;
  xSemaphoreGive(this->data_mutex_);
  ESP_LOGI(TAG, "Discovered %u Huawei topology devices", static_cast<unsigned>(devices.size()));
}

void HuaweiEmmaReverse::initialize_subscriptions_() {
  for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++)
    this->values_[index].subscribed = GENERATED_ENTITIES[index].enabled_default;
}

size_t HuaweiEmmaReverse::find_entity_(const std::string &name) const {
  for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++)
    if (name == GENERATED_ENTITIES[index].register_name)
      return index;
  return GENERATED_ENTITY_COUNT;
}

uint8_t HuaweiEmmaReverse::unit_for_role_(const char *role, bool &available) const {
  if (std::strcmp(role, "emma") == 0 || role[0] == '\0') {
    available = true;
    return 0;
  }
  for (const auto &device : this->topology_) {
    if (device.role == role) {
      available = true;
      return device.unit_id;
    }
  }
  available = false;
  return 0;
}

void HuaweiEmmaReverse::poll_group_(const char *group) {
  size_t subscribed = 0;
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++) {
    if (this->values_[index].subscribed && std::strcmp(GENERATED_ENTITIES[index].poll_group, group) == 0)
      subscribed++;
  }
  xSemaphoreGive(this->data_mutex_);
  size_t requested = 0;
  size_t batches = 0;
  const char *roles[] = {"emma", "inverter", "smartguard", "charger", "sdongle", "smartlogger"};
  for (const char *role : roles) {
    bool available = false;
    uint8_t unit = this->unit_for_role_(role, available);
    if (!available)
      continue;
    std::vector<size_t> indices;
    xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
    for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++) {
      const auto &metadata = GENERATED_ENTITIES[index];
      if (this->values_[index].subscribed && !this->values_[index].unsupported &&
          std::strcmp(metadata.poll_group, group) == 0 && std::strcmp(metadata.client_role, role) == 0)
        indices.push_back(index);
    }
    xSemaphoreGive(this->data_mutex_);
    requested += indices.size();
    size_t begin = 0;
    while (begin < indices.size()) {
      size_t end = begin + 1;
      const auto &first = GENERATED_ENTITIES[indices[begin]];
      uint32_t block_end = first.address + first.length;
      const bool structured = first.value_type == GeneratedValueType::TOU_HUAWEI ||
                              first.value_type == GeneratedValueType::TOU_LG ||
                              first.value_type == GeneratedValueType::CHARGE_PERIODS ||
                              first.value_type == GeneratedValueType::PEAK_PERIODS;
      if (!structured) {
        while (end < indices.size()) {
          const auto &next = GENERATED_ENTITIES[indices[end]];
          uint32_t candidate_end = next.address + next.length;
          if (next.address > block_end || candidate_end - first.address > GENERATED_MAX_READ_REGISTERS)
            break;
          block_end = std::max(block_end, candidate_end);
          end++;
        }
      }
      this->poll_entity_batch_(unit, indices, begin, end);
      batches++;
      begin = end;
    }
  }
  if (subscribed)
    ESP_LOGI(TAG, "EMMA poll group=%s subscribed=%u requested=%u batches=%u", group,
             static_cast<unsigned>(subscribed), static_cast<unsigned>(requested), static_cast<unsigned>(batches));
}

void HuaweiEmmaReverse::poll_entity_batch_(uint8_t unit, const std::vector<size_t> &indices, size_t begin,
                                           size_t end) {
  const auto &first = GENERATED_ENTITIES[indices[begin]];
  uint16_t block_end = first.address + first.length;
  for (size_t position = begin + 1; position < end; position++) {
    const auto &metadata = GENERATED_ENTITIES[indices[position]];
    block_end = std::max<uint16_t>(block_end, metadata.address + metadata.length);
  }
  std::vector<uint16_t> registers;
  xSemaphoreTake(this->tls_mutex_, portMAX_DELAY);
  const bool ok = this->read_registers_(unit, first.address, block_end - first.address, registers);
  xSemaphoreGive(this->tls_mutex_);
  if (!ok) {
    if (end - begin > 1) {
      for (size_t position = begin; position < end; position++)
        this->poll_entity_batch_(unit, indices, position, position + 1);
    } else if (this->connected_.load()) {
      xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
      this->values_[indices[begin]].unsupported = true;
      this->values_[indices[begin]].valid = false;
      xSemaphoreGive(this->data_mutex_);
      ESP_LOGW(TAG, "Register unsupported unit=%u name=%s address=%u count=%u", unit, first.register_name,
               first.address, first.length);
    }
    return;
  }
  const uint32_t now = millis();
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  for (size_t position = begin; position < end; position++) {
    size_t index = indices[position];
    std::string json;
    if (this->decode_entity_(index, registers, GENERATED_ENTITIES[index].address - first.address, json)) {
      this->values_[index].json = std::move(json);
      this->values_[index].valid = true;
      this->values_[index].updated_ms = now;
    } else {
      this->values_[index].valid = false;
    }
  }
  this->last_poll_ms_ = now;
  this->last_error_.clear();
  xSemaphoreGive(this->data_mutex_);
}

bool HuaweiEmmaReverse::decode_entity_(size_t index, const std::vector<uint16_t> &values, size_t offset,
                                       std::string &json) {
  const auto &metadata = GENERATED_ENTITIES[index];
  if (offset + metadata.length > values.size())
    return false;
  if (metadata.value_type == GeneratedValueType::STRING) {
    std::string text;
    for (size_t i = 0; i < metadata.length; i++) {
      char high = values[offset + i] >> 8;
      char low = values[offset + i] & 0xFF;
      if (high) text.push_back(high);
      if (low) text.push_back(low);
    }
    json = '"' + json_escape_(text) + '"';
    return true;
  }
  if (metadata.value_type == GeneratedValueType::TOU_HUAWEI) {
    std::vector<uint16_t> structured(values.begin() + offset, values.begin() + offset + metadata.length);
    json = this->tou_json_(decode_tou_(structured));
    return true;
  }
  if (metadata.value_type == GeneratedValueType::TOU_LG ||
      metadata.value_type == GeneratedValueType::CHARGE_PERIODS ||
      metadata.value_type == GeneratedValueType::PEAK_PERIODS) {
    std::vector<uint16_t> structured(values.begin() + offset, values.begin() + offset + metadata.length);
    json = this->decode_structured_(metadata, structured);
    return !json.empty();
  }
  uint64_t unsigned_raw = 0;
  int64_t signed_raw = 0;
  switch (metadata.value_type) {
    case GeneratedValueType::U16: unsigned_raw = values[offset]; break;
    case GeneratedValueType::I16: signed_raw = static_cast<int16_t>(values[offset]); break;
    case GeneratedValueType::U32:
    case GeneratedValueType::TIMESTAMP: unsigned_raw = u32_(values[offset], values[offset + 1]); break;
    case GeneratedValueType::I32:
    case GeneratedValueType::I32_ABSOLUTE: signed_raw = i32_(values[offset], values[offset + 1]); break;
    case GeneratedValueType::U64: unsigned_raw = u64_(values, offset); break;
    case GeneratedValueType::I64: signed_raw = static_cast<int64_t>(u64_(values, offset)); break;
    default: return false;
  }
  const bool unsigned_type = metadata.value_type == GeneratedValueType::U16 ||
                             metadata.value_type == GeneratedValueType::U32 ||
                             metadata.value_type == GeneratedValueType::U64 ||
                             metadata.value_type == GeneratedValueType::TIMESTAMP;
  if (metadata.has_invalid && (unsigned_type ? unsigned_raw == metadata.invalid_raw
                                             : signed_raw == static_cast<int64_t>(metadata.invalid_raw)))
    return false;
  int64_t raw = (metadata.value_type == GeneratedValueType::U16 || metadata.value_type == GeneratedValueType::U32 ||
                 metadata.value_type == GeneratedValueType::U64 || metadata.value_type == GeneratedValueType::TIMESTAMP)
                    ? static_cast<int64_t>(unsigned_raw) : signed_raw;
  if (metadata.value_type == GeneratedValueType::I32_ABSOLUTE && raw < 0)
    raw = -raw;
  if (metadata.unit_kind == GeneratedUnitKind::BOOL) {
    json = raw ? "true" : "false";
    return true;
  }
  if (metadata.unit_kind == GeneratedUnitKind::ENUM || metadata.unit_kind == GeneratedUnitKind::MAP) {
    for (size_t i = metadata.mapping_start; i < metadata.mapping_start + metadata.mapping_count; i++) {
      if (GENERATED_MAPPINGS[i].raw == raw) {
        const char *value = metadata.unit_kind == GeneratedUnitKind::ENUM ? GENERATED_MAPPINGS[i].key
                                                                          : GENERATED_MAPPINGS[i].label;
        json = '"' + json_escape_(value) + '"';
        return true;
      }
    }
  }
  if (metadata.unit_kind == GeneratedUnitKind::BITFIELD) {
    std::string text;
    for (size_t i = metadata.mapping_start; i < metadata.mapping_start + metadata.mapping_count; i++) {
      const auto &mapping = GENERATED_MAPPINGS[i];
      const char *label = (raw & mapping.raw) ? mapping.label : mapping.off_label;
      if (label[0] == '\0') continue;
      if (!text.empty()) text += "; ";
      text += label;
    }
    json = '"' + json_escape_(text) + '"';
    return true;
  }
  json = number_(static_cast<double>(raw) / metadata.gain);
  return true;
}

std::string HuaweiEmmaReverse::decode_structured_(const GeneratedEntityMetadata &metadata,
                                                  const std::vector<uint16_t> &values) {
  if (values.empty() || values[0] > 14)
    return "";
  std::ostringstream out;
  out << '[';
  if (metadata.value_type == GeneratedValueType::TOU_LG || metadata.value_type == GeneratedValueType::CHARGE_PERIODS) {
    if (1 + values[0] * 4 > values.size()) return "";
    for (size_t i = 0; i < values[0]; i++) {
      if (i) out << ',';
      size_t base = 1 + i * 4;
      int32_t amount = i32_(values[base + 2], values[base + 3]);
      out << "{\"start_time\":" << values[base] << ",\"end_time\":" << values[base + 1]
          << (metadata.value_type == GeneratedValueType::TOU_LG ? ",\"electricity_price\":" : ",\"power\":")
          << (metadata.value_type == GeneratedValueType::TOU_LG ? number_(amount / 1000.0) : std::to_string(amount)) << '}';
    }
  } else if (metadata.value_type == GeneratedValueType::PEAK_PERIODS) {
    std::vector<uint8_t> bytes;
    bytes.reserve(values.size() * 2);
    for (uint16_t value : values) { bytes.push_back(value >> 8); bytes.push_back(value); }
    if (2 + values[0] * 9 > bytes.size()) return "";
    for (size_t i = 0; i < values[0]; i++) {
      size_t base = 2 + i * 9;
      uint16_t start = bytes[base] << 8 | bytes[base + 1];
      uint16_t end = bytes[base + 2] << 8 | bytes[base + 3];
      int32_t power = static_cast<int32_t>(static_cast<uint32_t>(bytes[base + 4]) << 24 |
                                           static_cast<uint32_t>(bytes[base + 5]) << 16 |
                                           static_cast<uint32_t>(bytes[base + 6]) << 8 | bytes[base + 7]);
      uint8_t days = bytes[base + 8];
      if (start == end || days == 0) continue;
      if (out.tellp() > 1) out << ',';
      out << "{\"start_time\":" << start << ",\"end_time\":" << end << ",\"power\":" << power
          << ",\"days_effective\":[";
      for (uint8_t day = 0; day < 7; day++) { if (day) out << ','; out << ((days & (1U << day)) ? "true" : "false"); }
      out << "]}";
    }
  } else {
    return "";
  }
  out << ']';
  return out.str();
}

std::vector<uint16_t> HuaweiEmmaReverse::encode_tou_(const std::vector<TouPeriod> &periods) {
  std::vector<uint16_t> result(1 + GENERATED_TOU_MAX_PERIODS * 3, 0);
  result[0] = periods.size();
  for (size_t index = 0; index < periods.size() && index < GENERATED_TOU_MAX_PERIODS; index++) {
    result[1 + index * 3] = periods[index].start;
    result[2 + index * 3] = periods[index].end;
    result[3 + index * 3] = static_cast<uint16_t>(periods[index].charge_flag << 8 | periods[index].days);
  }
  return result;
}

std::vector<TouPeriod> HuaweiEmmaReverse::decode_tou_(const std::vector<uint16_t> &registers) {
  std::vector<TouPeriod> periods;
  if (registers.size() < 1 + GENERATED_TOU_MAX_PERIODS * 3 || registers[0] > GENERATED_TOU_MAX_PERIODS)
    return periods;
  for (size_t index = 0; index < registers[0]; index++) {
    TouPeriod period;
    period.start = registers[1 + index * 3];
    period.end = registers[2 + index * 3];
    period.charge_flag = registers[3 + index * 3] >> 8;
    period.days = registers[3 + index * 3] & 0x7F;
    periods.push_back(period);
  }
  return periods;
}

bool HuaweiEmmaReverse::write_tou_(size_t entity_index, const std::vector<TouPeriod> &periods,
                                   std::vector<TouPeriod> &readback) {
  const auto &metadata = GENERATED_ENTITIES[entity_index];
  bool available = false;
  uint8_t unit = this->unit_for_role_(metadata.client_role, available);
  if (!available) return false;
  xSemaphoreTake(this->tls_mutex_, portMAX_DELAY);
  bool ok = this->write_registers_(unit, metadata.address, encode_tou_(periods));
  std::vector<uint16_t> values;
  if (ok)
    ok = this->read_registers_(unit, metadata.address, metadata.length, values);
  xSemaphoreGive(this->tls_mutex_);
  if (!ok)
    return false;
  readback = decode_tou_(values);
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  this->values_[entity_index].json = this->tou_json_(readback);
  this->values_[entity_index].valid = true;
  this->values_[entity_index].updated_ms = millis();
  xSemaphoreGive(this->data_mutex_);
  return true;
}

bool HuaweiEmmaReverse::encode_scalar_(size_t index, cJSON *value, std::vector<uint16_t> &registers,
                                       std::string &error) {
  const auto &metadata = GENERATED_ENTITIES[index];
  double requested = 0;
  if (metadata.unit_kind == GeneratedUnitKind::BOOL) {
    if (!cJSON_IsBool(value)) { error = "value must be true or false"; return false; }
    requested = cJSON_IsTrue(value) ? 1 : 0;
  } else if (metadata.unit_kind == GeneratedUnitKind::ENUM) {
    bool matched = false;
    if (cJSON_IsString(value)) {
      std::string candidate(value->valuestring);
      std::transform(candidate.begin(), candidate.end(), candidate.begin(), ::tolower);
      for (size_t i = metadata.mapping_start; i < metadata.mapping_start + metadata.mapping_count; i++) {
        std::string key(GENERATED_MAPPINGS[i].key), label(GENERATED_MAPPINGS[i].label);
        std::transform(key.begin(), key.end(), key.begin(), ::tolower);
        std::transform(label.begin(), label.end(), label.begin(), ::tolower);
        if (candidate == key || candidate == label) {
          requested = GENERATED_MAPPINGS[i].raw;
          matched = true;
          break;
        }
      }
    } else if (cJSON_IsNumber(value)) {
      requested = value->valuedouble;
      for (size_t i = metadata.mapping_start; i < metadata.mapping_start + metadata.mapping_count; i++)
        matched |= GENERATED_MAPPINGS[i].raw == static_cast<int64_t>(requested);
    }
    if (!matched) { error = "value is not a supported option"; return false; }
  } else {
    if (!cJSON_IsNumber(value) || !std::isfinite(value->valuedouble)) {
      error = "value must be a finite number"; return false;
    }
    requested = value->valuedouble;
    if (metadata.has_range && (requested < metadata.minimum || requested > metadata.maximum)) {
      error = "value is outside the safe range"; return false;
    }
    const std::string name(metadata.register_name);
    if (name == "storage_maximum_charging_power" || name == "storage_maximum_discharging_power" ||
        name == "storage_forcible_charge_power" || name == "storage_forcible_discharge_power") {
      size_t rated_index = this->find_entity_("inverter_rated_power");
      double rated = 0;
      xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
      if (rated_index < GENERATED_ENTITY_COUNT && this->values_[rated_index].valid)
        rated = std::atof(this->values_[rated_index].json.c_str());
      xSemaphoreGive(this->data_mutex_);
      if (rated > 0 && requested > rated) {
        error = "value cannot exceed inverter rated power"; return false;
      }
    }
    requested *= metadata.gain;
  }
  int64_t raw = static_cast<int64_t>(std::llround(requested));
  registers.assign(metadata.length, 0);
  if (metadata.length == 1) {
    registers[0] = static_cast<uint16_t>(raw);
  } else if (metadata.length == 2) {
    uint32_t bits = static_cast<uint32_t>(raw);
    registers[0] = bits >> 16; registers[1] = bits;
  } else if (metadata.length == 4) {
    uint64_t bits = static_cast<uint64_t>(raw);
    for (size_t i = 0; i < 4; i++) registers[i] = bits >> (48 - i * 16);
  } else {
    error = "unsupported writable register width"; return false;
  }
  return true;
}

bool HuaweiEmmaReverse::write_entity_(size_t index, cJSON *value, std::string &readback, std::string &error) {
  const auto &metadata = GENERATED_ENTITIES[index];
  if (!metadata.writeable) { error = "register is read-only"; return false; }
  if (!this->connected_.load()) { error = "EMMA is not connected"; return false; }
  if (metadata.value_type == GeneratedValueType::TOU_HUAWEI) {
    char *encoded = cJSON_PrintUnformatted(value);
    std::string body = std::string("{\"periods\":") + (encoded == nullptr ? "[]" : encoded) + "}";
    cJSON_free(encoded);
    std::vector<TouPeriod> periods, result;
    if (!this->parse_tou_json_(body, periods, error)) return false;
    if (!this->write_tou_(index, periods, result)) { error = "TOU write/readback failed"; return false; }
    readback = this->tou_json_(result);
    return true;
  }
  std::vector<uint16_t> registers;
  if (!this->encode_scalar_(index, value, registers, error)) return false;
  bool available = false;
  uint8_t unit = this->unit_for_role_(metadata.client_role, available);
  if (!available) { error = std::string("device role is unavailable: ") + metadata.client_role; return false; }
  std::vector<uint16_t> returned;
  xSemaphoreTake(this->tls_mutex_, portMAX_DELAY);
  bool ok = registers.size() == 1 ? this->write_single_register_(unit, metadata.address, registers[0])
                                  : this->write_registers_(unit, metadata.address, registers);
  if (ok) ok = this->read_registers_(unit, metadata.address, metadata.length, returned);
  xSemaphoreGive(this->tls_mutex_);
  if (!ok) { error = "Modbus write/readback failed"; return false; }
  if (!this->decode_entity_(index, returned, 0, readback)) { error = "readback could not be decoded"; return false; }
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  this->values_[index].json = readback;
  this->values_[index].valid = true;
  this->values_[index].updated_ms = millis();
  xSemaphoreGive(this->data_mutex_);
  ESP_LOGI(TAG, "Control accepted register=%s unit=%u address=%u value=%s", metadata.register_name, unit,
           metadata.address, readback.c_str());
  return true;
}

bool HuaweiEmmaReverse::start_http_server_() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = this->api_port_;
  config.max_uri_handlers = 9;
  config.stack_size = 10240;
  config.lru_purge_enable = true;
  config.uri_match_fn = httpd_uri_match_wildcard;
  if (httpd_start(&this->http_server_, &config) != ESP_OK) {
    ESP_LOGE(TAG, "Could not start connector API on port %u", this->api_port_);
    return false;
  }
  const std::array<std::pair<const char *, httpd_method_t>, 7> routes{{
      {GENERATED_PATH_HEALTH, HTTP_GET}, {GENERATED_PATH_DEVICE, HTTP_GET}, {GENERATED_PATH_ENTITIES, HTTP_GET},
      {GENERATED_PATH_STATES, HTTP_GET}, {GENERATED_PATH_SUBSCRIPTIONS, HTTP_POST}, {GENERATED_PATH_TOU, HTTP_POST},
      {GENERATED_PATH_ENTITY_WILDCARD, HTTP_POST},
  }};
  for (const auto &route : routes) {
    httpd_uri_t uri{};
    uri.uri = route.first;
    uri.method = route.second;
    uri.handler = &HuaweiEmmaReverse::http_dispatch_;
    uri.user_ctx = this;
    if (httpd_register_uri_handler(this->http_server_, &uri) != ESP_OK)
      return false;
  }
  ESP_LOGI(TAG, "Authenticated connector API ready on port %u", this->api_port_);
  return true;
}

esp_err_t HuaweiEmmaReverse::http_dispatch_(httpd_req_t *request) {
  return static_cast<HuaweiEmmaReverse *>(request->user_ctx)->handle_http_(request);
}

esp_err_t HuaweiEmmaReverse::handle_http_(httpd_req_t *request) {
  this->signal_activity_(ActivityKind::API_RX);
  const char *method = request->method == HTTP_GET ? "GET" : request->method == HTTP_POST ? "POST" : "OTHER";
  if (!this->authenticate_(request)) {
    ESP_LOGW(TAG, "Connector API authentication rejected method=%s path=%s", method, request->uri);
    return this->send_json_(request, "{\"error\":\"unauthorized\"}", "401 Unauthorized");
  }
  ESP_LOGD(TAG, "Connector API request accepted method=%s path=%s", method, request->uri);
  const std::string uri(request->uri);
  if (request->method == HTTP_GET && uri == GENERATED_PATH_HEALTH)
    return this->send_json_(request, this->health_json_());
  if (request->method == HTTP_GET && uri == GENERATED_PATH_DEVICE)
    return this->send_json_(request, this->device_json_());
  if (request->method == HTTP_GET && uri == GENERATED_PATH_ENTITIES)
    return this->send_entities_json_(request);
  if (request->method == HTTP_GET && uri == GENERATED_PATH_STATES)
    return this->send_json_(request, this->states_json_());
  if (request->method == HTTP_POST && uri == GENERATED_PATH_SUBSCRIPTIONS) {
    std::string body = this->read_http_body_(request);
    cJSON *root = cJSON_Parse(body.c_str());
    cJSON *names = root == nullptr ? nullptr : cJSON_GetObjectItem(root, "register_names");
    if (!cJSON_IsArray(names)) {
      cJSON_Delete(root);
      return this->send_json_(request, "{\"error\":\"register_names must be an array\"}", "400 Bad Request");
    }
    std::vector<size_t> accepted;
    cJSON *name;
    cJSON_ArrayForEach(name, names) {
      if (!cJSON_IsString(name)) continue;
      size_t index = this->find_entity_(name->valuestring);
      if (index < GENERATED_ENTITY_COUNT &&
          std::find(accepted.begin(), accepted.end(), index) == accepted.end())
        accepted.push_back(index);
    }
    std::vector<size_t> added;
    std::vector<size_t> removed;
    xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
    for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++) {
      bool selected = std::find(accepted.begin(), accepted.end(), index) != accepted.end();
      if (selected && !this->values_[index].subscribed) added.push_back(index);
      if (!selected && this->values_[index].subscribed) removed.push_back(index);
      this->values_[index].subscribed = selected;
      if (!selected) { this->values_[index].valid = false; this->values_[index].json.clear(); }
    }
    xSemaphoreGive(this->data_mutex_);
    this->subscriptions_changed_.store(true);
    std::ostringstream out;
    out << "{\"register_names\":[";
    for (size_t position = 0; position < accepted.size(); position++) {
      if (position) out << ',';
      out << '"' << GENERATED_ENTITIES[accepted[position]].register_name << '"';
    }
    out << "]}";
    cJSON_Delete(root);
    ESP_LOGI(TAG, "Polling subscriptions updated total=%u added=%u removed=%u",
             static_cast<unsigned>(accepted.size()), static_cast<unsigned>(added.size()),
             static_cast<unsigned>(removed.size()));
    for (size_t index : added)
      ESP_LOGI(TAG, "Polling subscription added group=%s name=%s", GENERATED_ENTITIES[index].poll_group,
               GENERATED_ENTITIES[index].register_name);
    for (size_t index : removed)
      ESP_LOGI(TAG, "Polling subscription removed group=%s name=%s", GENERATED_ENTITIES[index].poll_group,
               GENERATED_ENTITIES[index].register_name);
    const char *subscription_groups[] = {"fast", "medium", "slow"};
    for (const char *subscription_group : subscription_groups) {
      size_t count = 0;
      for (size_t index : accepted)
        if (std::strcmp(GENERATED_ENTITIES[index].poll_group, subscription_group) == 0) count++;
      ESP_LOGI(TAG, "Polling subscription group=%s active=%u", subscription_group, static_cast<unsigned>(count));
    }
    return this->send_json_(request, out.str());
  }
  if (request->method == HTTP_POST && uri == GENERATED_PATH_TOU) {
    std::vector<TouPeriod> periods;
    std::string error;
    if (!this->parse_tou_json_(this->read_http_body_(request), periods, error))
      return this->send_json_(request, "{\"error\":\"" + json_escape_(error) + "\"}", "400 Bad Request");
    std::vector<TouPeriod> readback;
    size_t index = this->find_entity_("emma_tou_periods");
    if (index == GENERATED_ENTITY_COUNT || !this->write_tou_(index, periods, readback))
      return this->send_json_(request, "{\"error\":\"EMMA TOU write/readback failed\"}", "503 Service Unavailable");
    return this->send_json_(request, "{\"value\":" + this->tou_json_(readback) + "}");
  }
  const std::string prefix = std::string(GENERATED_PATH_ENTITIES) + "/";
  const std::string suffix = "/value";
  if (request->method == HTTP_POST && uri.rfind(prefix, 0) == 0 && uri.size() > prefix.size() + suffix.size() &&
      uri.compare(uri.size() - suffix.size(), suffix.size(), suffix) == 0) {
    std::string name = uri.substr(prefix.size(), uri.size() - prefix.size() - suffix.size());
    size_t index = this->find_entity_(name);
    if (index == GENERATED_ENTITY_COUNT)
      return this->send_json_(request, "{\"error\":\"unknown register\"}", "404 Not Found");
    std::string body = this->read_http_body_(request);
    cJSON *root = cJSON_Parse(body.c_str());
    cJSON *value = root == nullptr ? nullptr : cJSON_GetObjectItem(root, "value");
    if (value == nullptr) { cJSON_Delete(root); return this->send_json_(request, "{\"error\":\"value is required\"}", "400 Bad Request"); }
    std::string readback, error;
    bool ok = this->write_entity_(index, value, readback, error);
    cJSON_Delete(root);
    if (!ok)
      return this->send_json_(request, "{\"error\":\"" + json_escape_(error) + "\"}",
                              this->connected_.load() ? "400 Bad Request" : "503 Service Unavailable");
    return this->send_json_(request, "{\"value\":" + readback + "}");
  }
  return this->send_json_(request, "{\"error\":\"not found\"}", "404 Not Found");
}

bool HuaweiEmmaReverse::authenticate_(httpd_req_t *request) {
  const size_t length = httpd_req_get_hdr_value_len(request, "Authorization");
  if (length == 0 || length > 256)
    return false;
  std::vector<char> header(length + 1);
  if (httpd_req_get_hdr_value_str(request, "Authorization", header.data(), header.size()) != ESP_OK)
    return false;
  const std::string expected = "Bearer " + this->api_token_;
  if (expected.size() != length)
    return false;
  uint8_t difference = 0;
  for (size_t index = 0; index < expected.size(); index++)
    difference |= static_cast<uint8_t>(expected[index] ^ header[index]);
  return difference == 0;
}

std::string HuaweiEmmaReverse::read_http_body_(httpd_req_t *request) {
  if (request->content_len <= 0 || request->content_len > GENERATED_MAX_HTTP_BODY)
    return "";
  std::string body(request->content_len, '\0');
  size_t offset = 0;
  while (offset < body.size()) {
    int received = httpd_req_recv(request, body.data() + offset, body.size() - offset);
    if (received <= 0)
      return "";
    offset += received;
  }
  return body;
}

esp_err_t HuaweiEmmaReverse::send_json_(httpd_req_t *request, const std::string &json, const char *status) {
  this->signal_activity_(ActivityKind::API_TX);
  ESP_LOGD(TAG, "Connector API response path=%s status=%s bytes=%u", request->uri, status,
           static_cast<unsigned>(json.size()));
  httpd_resp_set_status(request, status);
  httpd_resp_set_type(request, "application/json");
  return httpd_resp_send(request, json.c_str(), json.size());
}

esp_err_t HuaweiEmmaReverse::send_entities_json_(httpd_req_t *request) {
  this->signal_activity_(ActivityKind::API_TX);
  httpd_resp_set_type(request, "application/json");
  std::string opening = std::string("{\"contract_version\":") + std::to_string(GENERATED_CONTRACT_VERSION) +
                        ",\"catalog_sha256\":\"" + GENERATED_CATALOG_SHA256 + "\",\"entities\":[";
  if (httpd_resp_send_chunk(request, opening.c_str(), opening.size()) != ESP_OK) return ESP_FAIL;
  double rated_power = 0;
  size_t rated_index = this->find_entity_("inverter_rated_power");
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  if (rated_index < GENERATED_ENTITY_COUNT && this->values_[rated_index].valid)
    rated_power = std::atof(this->values_[rated_index].json.c_str());
  xSemaphoreGive(this->data_mutex_);
  for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++) {
    const auto &metadata = GENERATED_ENTITIES[index];
    auto value = [](const char *text) {
      return text == nullptr || text[0] == '\0' ? std::string("null") :
             std::string("\"") + HuaweiEmmaReverse::json_escape_(text) + "\"";
    };
    std::ostringstream out;
    if (index) out << ',';
    out << "{\"register_name\":\"" << metadata.register_name << "\",\"esphome_id\":\"" << metadata.esphome_id
        << "\",\"name\":\"" << json_escape_(metadata.name) << "\",\"address\":" << metadata.address
        << ",\"length\":" << metadata.length << ",\"platform\":\"" << metadata.platform
        << "\",\"poll_group\":\"" << metadata.poll_group << "\",\"device_role\":\"" << metadata.device_role
        << "\",\"client_role\":\"" << metadata.client_role << "\",\"unit\":" << value(metadata.unit)
        << ",\"device_class\":" << value(metadata.device_class) << ",\"state_class\":" << value(metadata.state_class)
        << ",\"entity_category\":" << value(metadata.entity_category) << ",\"icon\":" << value(metadata.icon)
        << ",\"enabled_default\":" << (metadata.enabled_default ? "true" : "false")
        << ",\"writeable\":" << (metadata.writeable ? "true" : "false") << ",\"format\":" << value(metadata.format);
    if (metadata.has_range) {
      const std::string name(metadata.register_name);
      const bool rated_limited = name == "storage_maximum_charging_power" ||
                                  name == "storage_maximum_discharging_power" ||
                                  name == "storage_forcible_charge_power" ||
                                  name == "storage_forcible_discharge_power";
      const double maximum = rated_limited && rated_power > 0 ? rated_power : metadata.maximum;
      out << ",\"minimum\":" << number_(metadata.minimum) << ",\"maximum\":" << number_(maximum)
          << ",\"step\":" << number_(metadata.step);
    }
    if (metadata.unit_kind == GeneratedUnitKind::ENUM) {
      out << ",\"options\":[";
      for (size_t i = metadata.mapping_start; i < metadata.mapping_start + metadata.mapping_count; i++) {
        if (i != metadata.mapping_start) out << ',';
        const auto &mapping = GENERATED_MAPPINGS[i];
        out << "{\"value\":" << mapping.raw << ",\"key\":\"" << json_escape_(mapping.key)
            << "\",\"label\":\"" << json_escape_(mapping.label) << "\"}";
      }
      out << ']';
    } else {
      out << ",\"options\":[]";
    }
    out << '}';
    std::string chunk = out.str();
    if (httpd_resp_send_chunk(request, chunk.c_str(), chunk.size()) != ESP_OK) return ESP_FAIL;
  }
  const char closing[] = "]}";
  ESP_LOGD(TAG, "Connector API streamed catalog entities=%u", static_cast<unsigned>(GENERATED_ENTITY_COUNT));
  httpd_resp_send_chunk(request, closing, sizeof(closing) - 1);
  return httpd_resp_send_chunk(request, nullptr, 0);
}

std::string HuaweiEmmaReverse::health_json_() {
  std::ostringstream out;
  out << "{\"connected\":" << (this->connected_.load() ? "true" : "false")
      << ",\"tls_port\":" << this->tls_port_ << ",\"api_port\":" << this->api_port_
      << ",\"registers_available\":";
  size_t available = 0;
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  size_t unsupported = 0, subscribed = 0;
  for (const auto &value : this->values_) {
    available += value.valid ? 1 : 0;
    unsupported += value.unsupported ? 1 : 0;
    subscribed += value.subscribed ? 1 : 0;
  }
  out << available << ",\"registers_subscribed\":" << subscribed << ",\"registers_unsupported\":" << unsupported
      << ",\"reconnect_count\":" << this->reconnect_count_ << ",\"connected_at_ms\":" << this->connected_at_ms_
      << ",\"last_poll_ms\":" << this->last_poll_ms_ << ",\"last_error\":\""
      << json_escape_(this->last_error_) << "\"}";
  xSemaphoreGive(this->data_mutex_);
  return out.str();
}

std::string HuaweiEmmaReverse::device_json_() {
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  std::ostringstream out;
  out << "{\"manufacturer\":\"Huawei\",\"model\":\"" << json_escape_(this->emma_.model.empty() ? "EMMA" : this->emma_.model)
      << "\",\"serial_number\":\"" << json_escape_(this->emma_.serial) << "\",\"sw_version\":\""
      << json_escape_(this->emma_.firmware) << "\",\"protocol_version\":\"" << json_escape_(this->emma_.protocol)
      << "\",\"product_type\":\"" << json_escape_(this->emma_.product_type) << "\",\"devices\":[";
  for (size_t index = 0; index < this->topology_.size(); index++) {
    const auto &device = this->topology_[index];
    if (index) out << ',';
    out << "{\"object_id\":\"0x";
    char id[3]; std::snprintf(id, sizeof(id), "%02X", device.object_id); out << id;
    out << "\",\"role\":\"" << json_escape_(device.role) << "\",\"model\":\"" << json_escape_(device.model)
        << "\",\"sw_version\":\"" << json_escape_(device.firmware) << "\",\"serial_number\":\""
        << json_escape_(device.serial) << "\",\"unit_id\":" << static_cast<unsigned>(device.unit_id) << "}";
  }
  out << "],\"topology\":{}}";
  xSemaphoreGive(this->data_mutex_);
  return out.str();
}

std::string HuaweiEmmaReverse::entities_json_() {
  std::ostringstream out;
  out << "{\"catalog_sha256\":\"" << GENERATED_CATALOG_SHA256 << "\",\"entities\":[";
  for (size_t index = 0; index < GENERATED_ENTITIES.size(); index++) {
    const auto &metadata = GENERATED_ENTITIES[index];
    if (index)
      out << ',';
    auto nullable = [&out](const char *value) {
      if (value == nullptr || value[0] == '\0')
        out << "null";
      else
        out << '\"' << HuaweiEmmaReverse::json_escape_(value) << '\"';
    };
    out << "{\"register_name\":\"" << metadata.register_name << "\",\"esphome_id\":\""
        << metadata.esphome_id << "\",\"name\":\"" << json_escape_(metadata.name)
        << "\",\"address\":" << metadata.address << ",\"length\":" << metadata.length
        << ",\"platform\":\"" << metadata.platform << "\",\"poll_group\":\""
        << metadata.poll_group << "\",\"device_role\":\"" << metadata.device_role
        << "\",\"client_role\":\"" << metadata.client_role << "\",\"unit\":";
    nullable(metadata.unit);
    out << ",\"device_class\":";
    nullable(metadata.device_class);
    out << ",\"state_class\":";
    nullable(metadata.state_class);
    out << ",\"entity_category\":";
    nullable(metadata.entity_category);
    out << ",\"icon\":";
    nullable(metadata.icon);
    out << ",\"enabled_default\":" << (metadata.enabled_default ? "true" : "false")
        << ",\"writeable\":" << (metadata.writeable ? "true" : "false") << ",\"format\":";
    nullable(metadata.format);
    out << '}';
  }
  out << "]}";
  return out.str();
}

std::string HuaweiEmmaReverse::states_json_() {
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  std::ostringstream out;
  out << "{\"values\":{";
  bool first = true;
  for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++) {
    if (!this->values_[index].valid || !this->values_[index].subscribed) continue;
    if (!first) out << ',';
    first = false;
    out << '\"' << GENERATED_ENTITIES[index].register_name << "\":" << this->values_[index].json;
  }
  out << "},\"updated_at\":{";
  first = true;
  for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++) {
    if (!this->values_[index].valid || !this->values_[index].subscribed) continue;
    if (!first) out << ',';
    first = false;
    out << '\"' << GENERATED_ENTITIES[index].register_name << "\":\"" << iso_time_(this->values_[index].updated_ms) << '\"';
  }
  out << "},\"unsupported\":[";
  first = true;
  for (size_t index = 0; index < GENERATED_ENTITY_COUNT; index++) {
    if (!this->values_[index].unsupported || !this->values_[index].subscribed) continue;
    if (!first) out << ',';
    first = false;
    out << '\"' << GENERATED_ENTITIES[index].register_name << '\"';
  }
  out << "]}";
  xSemaphoreGive(this->data_mutex_);
  return out.str();
}

std::string HuaweiEmmaReverse::tou_json_(const std::vector<TouPeriod> &periods) {
  std::ostringstream out;
  out << '[';
  for (size_t index = 0; index < periods.size(); index++) {
    if (index) out << ',';
    const TouPeriod &period = periods[index];
    out << "{\"start_time\":" << period.start << ",\"end_time\":" << period.end
        << ",\"action\":\"" << (period.charge_flag == 0 ? "charge" : "discharge") << "\",\"days\":[";
    for (uint8_t day = 0; day < 7; day++) {
      if (day) out << ',';
      out << ((period.days & (1U << day)) ? "true" : "false");
    }
    out << "]}";
  }
  out << ']';
  return out.str();
}

static bool parse_time(cJSON *value, uint16_t &minutes) {
  if (cJSON_IsNumber(value)) {
    if (value->valuedouble < 0 || value->valuedouble > 1440) return false;
    minutes = static_cast<uint16_t>(value->valueint);
    return true;
  }
  if (!cJSON_IsString(value)) return false;
  unsigned hour, minute;
  if (std::sscanf(value->valuestring, "%u:%u", &hour, &minute) != 2 || hour > 24 || minute > 59 ||
      (hour == 24 && minute != 0)) return false;
  minutes = static_cast<uint16_t>(hour * 60 + minute);
  return true;
}

bool HuaweiEmmaReverse::parse_tou_json_(const std::string &body, std::vector<TouPeriod> &periods,
                                        std::string &error) {
  cJSON *root = cJSON_Parse(body.c_str());
  cJSON *items = root == nullptr ? nullptr : cJSON_GetObjectItem(root, "periods");
  if (!cJSON_IsArray(items) || cJSON_GetArraySize(items) > GENERATED_TOU_MAX_PERIODS) {
    error = "periods must be a list containing at most 14 items";
    cJSON_Delete(root);
    return false;
  }
  cJSON *item;
  cJSON_ArrayForEach(item, items) {
    TouPeriod period;
    if (!cJSON_IsObject(item) || !parse_time(cJSON_GetObjectItem(item, "start_time"), period.start) ||
        !parse_time(cJSON_GetObjectItem(item, "end_time"), period.end) || period.start >= period.end) {
      error = "each period must have valid start_time < end_time";
      cJSON_Delete(root);
      return false;
    }
    cJSON *action = cJSON_GetObjectItem(item, "action");
    if (action == nullptr) action = cJSON_GetObjectItem(item, "charge_flag");
    if (cJSON_IsString(action)) {
      std::string mode(action->valuestring);
      if (mode == "charge" || mode == "c" || mode == "0") period.charge_flag = 0;
      else if (mode == "discharge" || mode == "d" || mode == "1") period.charge_flag = 1;
      else { error = "charge_flag/action must be charge or discharge"; cJSON_Delete(root); return false; }
    } else if (cJSON_IsNumber(action) && (action->valueint == 0 || action->valueint == 1)) {
      period.charge_flag = action->valueint;
    } else { error = "charge_flag/action is required"; cJSON_Delete(root); return false; }
    cJSON *days = cJSON_GetObjectItem(item, "days");
    if (days == nullptr) days = cJSON_GetObjectItem(item, "days_effective");
    if (!cJSON_IsArray(days) || cJSON_GetArraySize(days) != 7) {
      error = "days/days_effective must contain seven booleans"; cJSON_Delete(root); return false;
    }
    period.days = 0;
    for (int day = 0; day < 7; day++) {
      cJSON *enabled = cJSON_GetArrayItem(days, day);
      if (!cJSON_IsBool(enabled)) { error = "days must contain booleans"; cJSON_Delete(root); return false; }
      if (cJSON_IsTrue(enabled)) period.days |= 1U << day;
    }
    if (period.days == 0) { error = "a period must apply to at least one day"; cJSON_Delete(root); return false; }
    periods.push_back(period);
  }
  cJSON_Delete(root);
  for (uint8_t day = 0; day < 7; day++) {
    for (size_t left = 0; left < periods.size(); left++) {
      if ((periods[left].days & (1U << day)) == 0) continue;
      for (size_t right = left + 1; right < periods.size(); right++) {
        if ((periods[right].days & (1U << day)) != 0 && periods[left].start < periods[right].end &&
            periods[right].start < periods[left].end) {
          error = "TOU periods overlap on an enabled weekday";
          return false;
        }
      }
    }
  }
  return true;
}

void HuaweiEmmaReverse::signal_activity_(ActivityKind kind) {
  this->pending_activity_.fetch_or(static_cast<uint8_t>(1U << static_cast<uint8_t>(kind)));
}

void HuaweiEmmaReverse::update_activity_led_() {
  const uint32_t now = millis();
  if (this->led_on_ && static_cast<int32_t>(now - this->led_until_) >= 0) {
    this->activity_output_->turn_off();
    this->led_on_ = false;
    if (this->led_pulses_left_ > 0)
      this->led_until_ = now + 55;
  } else if (!this->led_on_ && this->led_pulses_left_ > 0 && static_cast<int32_t>(now - this->led_until_) >= 0) {
    this->activity_output_->turn_on();
    this->led_on_ = true;
    this->led_pulses_left_--;
    this->led_until_ = now + 55;
  }
  if (this->led_on_ || this->led_pulses_left_ > 0)
    return;
  uint8_t activity = this->pending_activity_.exchange(0);
  if (activity == 0)
    return;
  uint32_t duration = 40;
  uint8_t pulses = 1;
  if (activity & (1U << static_cast<uint8_t>(ActivityKind::API_TX))) duration = 220;
  else if (activity & (1U << static_cast<uint8_t>(ActivityKind::API_RX))) { duration = 55; pulses = 2; }
  else if (activity & (1U << static_cast<uint8_t>(ActivityKind::MODBUS_TX))) duration = 90;
  this->activity_output_->turn_on();
  this->led_on_ = true;
  this->led_pulses_left_ = pulses - 1;
  this->led_until_ = now + duration;
}

void HuaweiEmmaReverse::set_connected_(bool connected) {
  bool previous = this->connected_.exchange(connected);
  if (previous != connected)
    this->connected_sensor_->publish_state(connected);
}

void HuaweiEmmaReverse::set_last_error_(const std::string &error) {
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  this->last_error_ = error;
  xSemaphoreGive(this->data_mutex_);
}

std::string HuaweiEmmaReverse::json_escape_(const std::string &value) {
  std::string result;
  result.reserve(value.size());
  for (char character : value) {
    if (character == '\\' || character == '\"') result.push_back('\\');
    if (static_cast<unsigned char>(character) >= 0x20) result.push_back(character);
  }
  return result;
}

std::string HuaweiEmmaReverse::role_for_(const std::string &model, const std::string &product) {
  std::string text = model + " " + product;
  std::transform(text.begin(), text.end(), text.begin(), ::tolower);
  if (text.find("emma") != std::string::npos || text.find("hems") != std::string::npos) return "emma";
  if (text.find("smartguard") != std::string::npos || text.find("backupbox") != std::string::npos) return "smartguard";
  if (text.find("sun2000") != std::string::npos || text.find("inverter") != std::string::npos) return "inverter";
  if (text.find("charger") != std::string::npos) return "charger";
  if (text.find("sdongle") != std::string::npos || text.find("smart dongle") != std::string::npos) return "sdongle";
  if (text.find("smartlogger") != std::string::npos || text.find("smart logger") != std::string::npos) return "smartlogger";
  return "accessory";
}

uint32_t HuaweiEmmaReverse::u32_(uint16_t high, uint16_t low) { return static_cast<uint32_t>(high) << 16 | low; }
int32_t HuaweiEmmaReverse::i32_(uint16_t high, uint16_t low) { return static_cast<int32_t>(u32_(high, low)); }

uint64_t HuaweiEmmaReverse::u64_(const std::vector<uint16_t> &values, size_t offset) {
  uint64_t result = 0;
  for (size_t index = 0; index < 4; index++) result = (result << 16) | values[offset + index];
  return result;
}

std::string HuaweiEmmaReverse::iso_time_(uint32_t updated_ms) {
  std::time_t now = std::time(nullptr);
  if (now < 1600000000) return std::to_string(updated_ms);
  now -= static_cast<uint32_t>(millis() - updated_ms) / 1000;
  std::tm utc{};
  gmtime_r(&now, &utc);
  char buffer[32];
  std::strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &utc);
  return buffer;
}

std::string HuaweiEmmaReverse::number_(double value) {
  char buffer[32];
  if (std::fabs(value - std::round(value)) < 0.000001)
    std::snprintf(buffer, sizeof(buffer), "%.0f", value);
  else
    std::snprintf(buffer, sizeof(buffer), "%.3f", value);
  return buffer;
}

}  // namespace esphome::huawei_emma_reverse
