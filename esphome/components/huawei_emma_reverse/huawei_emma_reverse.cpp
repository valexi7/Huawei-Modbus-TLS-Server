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
static constexpr uint16_t CORE_ADDRESS = 30354;
static constexpr uint16_t CORE_COUNT = 20;
static constexpr uint16_t TOU_ADDRESS = 40004;
static constexpr uint16_t TOU_COUNT = 43;
static constexpr uint32_t FAST_POLL_MS = 30000;
static constexpr uint32_t TOU_POLL_MS = 300000;
static constexpr size_t MAX_MBAP_LENGTH = 512;

static const std::array<const char *, 10> CORE_NAMES{{
    "pv_output_power", "load_power", "feed_in_power", "battery_charge_discharge_power",
    "inverter_rated_power", "inverter_active_power", "state_of_capacity", "ess_chargeable_capacity",
    "ess_dischargeable_capacity", "backup_power_state_of_charge",
}};

static const std::array<uint16_t, 10> CORE_OFFSETS{{0, 2, 4, 6, 8, 10, 14, 15, 17, 19}};
static const std::array<bool, 10> CORE_SIGNED{{false, false, true, true, false, true, false, false, false, false}};
static const std::array<double, 10> CORE_GAINS{{1, 1, 1, 1, 1, 1, 100, 1000, 1000, 100}};

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
    this->poll_core_();
    this->poll_tou_();
    uint32_t next_fast = millis() + FAST_POLL_MS;
    uint32_t next_tou = millis() + TOU_POLL_MS;

    while (this->connected_.load()) {
      const uint32_t now = millis();
      if (static_cast<int32_t>(now - next_fast) >= 0) {
        this->poll_core_();
        next_fast = now + FAST_POLL_MS;
      }
      if (static_cast<int32_t>(now - next_tou) >= 0) {
        this->poll_tou_();
        next_tou = now + TOU_POLL_MS;
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
  if (length < 2 || length > MAX_MBAP_LENGTH) {
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
  if (values.empty() || values.size() > 123)
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

void HuaweiEmmaReverse::poll_core_() {
  std::vector<uint16_t> registers;
  xSemaphoreTake(this->tls_mutex_, portMAX_DELAY);
  const bool ok = this->read_registers_(0, CORE_ADDRESS, CORE_COUNT, registers);
  xSemaphoreGive(this->tls_mutex_);
  if (!ok)
    return;
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  for (size_t index = 0; index < CORE_NAMES.size(); index++) {
    const size_t offset = CORE_OFFSETS[index];
    int64_t raw;
    if (index < 6)
      raw = CORE_SIGNED[index] ? i32_(registers[offset], registers[offset + 1])
                               : u32_(registers[offset], registers[offset + 1]);
    else if (index == 6 || index == 9)
      raw = registers[offset];
    else
      raw = u32_(registers[offset], registers[offset + 1]);
    this->core_values_[index] = static_cast<double>(raw) / CORE_GAINS[index];
    this->core_valid_[index] = true;
  }
  this->last_poll_ms_ = millis();
  this->last_error_.clear();
  xSemaphoreGive(this->data_mutex_);
  ESP_LOGI(TAG, "EMMA core poll updated %u registers", static_cast<unsigned>(CORE_NAMES.size()));
}

void HuaweiEmmaReverse::poll_tou_() {
  std::vector<uint16_t> registers;
  xSemaphoreTake(this->tls_mutex_, portMAX_DELAY);
  const bool ok = this->read_registers_(0, TOU_ADDRESS, TOU_COUNT, registers);
  xSemaphoreGive(this->tls_mutex_);
  if (!ok)
    return;
  std::vector<TouPeriod> decoded = decode_tou_(registers);
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  this->tou_periods_ = decoded;
  this->tou_valid_ = true;
  xSemaphoreGive(this->data_mutex_);
  ESP_LOGI(TAG, "EMMA TOU readback contains %u periods", static_cast<unsigned>(decoded.size()));
}

std::vector<uint16_t> HuaweiEmmaReverse::encode_tou_(const std::vector<TouPeriod> &periods) {
  std::vector<uint16_t> result(TOU_COUNT, 0);
  result[0] = periods.size();
  for (size_t index = 0; index < periods.size() && index < 14; index++) {
    result[1 + index * 3] = periods[index].start;
    result[2 + index * 3] = periods[index].end;
    result[3 + index * 3] = static_cast<uint16_t>(periods[index].charge_flag << 8 | periods[index].days);
  }
  return result;
}

std::vector<TouPeriod> HuaweiEmmaReverse::decode_tou_(const std::vector<uint16_t> &registers) {
  std::vector<TouPeriod> periods;
  if (registers.size() != TOU_COUNT || registers[0] > 14)
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

bool HuaweiEmmaReverse::write_tou_(const std::vector<TouPeriod> &periods, std::vector<TouPeriod> &readback) {
  xSemaphoreTake(this->tls_mutex_, portMAX_DELAY);
  bool ok = this->write_registers_(0, TOU_ADDRESS, encode_tou_(periods));
  std::vector<uint16_t> values;
  if (ok)
    ok = this->read_registers_(0, TOU_ADDRESS, TOU_COUNT, values);
  xSemaphoreGive(this->tls_mutex_);
  if (!ok)
    return false;
  readback = decode_tou_(values);
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  this->tou_periods_ = readback;
  this->tou_valid_ = true;
  xSemaphoreGive(this->data_mutex_);
  return true;
}

bool HuaweiEmmaReverse::start_http_server_() {
  httpd_config_t config = HTTPD_DEFAULT_CONFIG();
  config.server_port = this->api_port_;
  config.max_uri_handlers = 8;
  config.stack_size = 10240;
  config.lru_purge_enable = true;
  if (httpd_start(&this->http_server_, &config) != ESP_OK) {
    ESP_LOGE(TAG, "Could not start connector API on port %u", this->api_port_);
    return false;
  }
  const std::array<std::pair<const char *, httpd_method_t>, 6> routes{{
      {"/api/v1/health", HTTP_GET}, {"/api/v1/device", HTTP_GET}, {"/api/v1/entities", HTTP_GET},
      {"/api/v1/states", HTTP_GET}, {"/api/v1/subscriptions", HTTP_POST}, {"/api/v1/tou-periods", HTTP_POST},
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
  if (!this->authenticate_(request))
    return this->send_json_(request, "{\"error\":\"unauthorized\"}", "401 Unauthorized");
  const std::string uri(request->uri);
  if (request->method == HTTP_GET && uri == "/api/v1/health")
    return this->send_json_(request, this->health_json_());
  if (request->method == HTTP_GET && uri == "/api/v1/device")
    return this->send_json_(request, this->device_json_());
  if (request->method == HTTP_GET && uri == "/api/v1/entities")
    return this->send_json_(request, this->entities_json_());
  if (request->method == HTTP_GET && uri == "/api/v1/states")
    return this->send_json_(request, this->states_json_());
  if (request->method == HTTP_POST && uri == "/api/v1/subscriptions") {
    std::string body = this->read_http_body_(request);
    cJSON *root = cJSON_Parse(body.c_str());
    cJSON *names = root == nullptr ? nullptr : cJSON_GetObjectItem(root, "register_names");
    if (!cJSON_IsArray(names)) {
      cJSON_Delete(root);
      return this->send_json_(request, "{\"error\":\"register_names must be an array\"}", "400 Bad Request");
    }
    char *encoded = cJSON_PrintUnformatted(names);
    std::string response = std::string("{\"register_names\":") + (encoded == nullptr ? "[]" : encoded) + "}";
    cJSON_free(encoded);
    cJSON_Delete(root);
    return this->send_json_(request, response);
  }
  if (request->method == HTTP_POST && uri == "/api/v1/tou-periods") {
    std::vector<TouPeriod> periods;
    std::string error;
    if (!this->parse_tou_json_(this->read_http_body_(request), periods, error))
      return this->send_json_(request, "{\"error\":\"" + json_escape_(error) + "\"}", "400 Bad Request");
    std::vector<TouPeriod> readback;
    if (!this->write_tou_(periods, readback))
      return this->send_json_(request, "{\"error\":\"EMMA TOU write/readback failed\"}", "503 Service Unavailable");
    return this->send_json_(request, "{\"value\":" + this->tou_json_(readback) + "}");
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
  if (request->content_len <= 0 || request->content_len > 8192)
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
  httpd_resp_set_status(request, status);
  httpd_resp_set_type(request, "application/json");
  return httpd_resp_send(request, json.c_str(), json.size());
}

std::string HuaweiEmmaReverse::health_json_() {
  std::ostringstream out;
  out << "{\"connected\":" << (this->connected_.load() ? "true" : "false")
      << ",\"tls_port\":" << this->tls_port_ << ",\"api_port\":" << this->api_port_
      << ",\"registers_available\":";
  size_t available = 0;
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  for (bool valid : this->core_valid_)
    available += valid ? 1 : 0;
  if (this->tou_valid_)
    available++;
  out << available << ",\"last_poll_ms\":" << this->last_poll_ms_ << ",\"last_error\":\""
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
  static const char *const JSON = R"json({"entities":[
{"register_name":"pv_output_power","name":"PV Output Power","address":30354,"length":2,"platform":"sensor","poll_group":"fast","device_role":"emma","client_role":"emma","unit":"W","device_class":"power","state_class":"measurement","icon":"mdi:solar-power","enabled_default":true,"writeable":false},
{"register_name":"load_power","name":"Load Power","address":30356,"length":2,"platform":"sensor","poll_group":"fast","device_role":"emma","client_role":"emma","unit":"W","device_class":"power","state_class":"measurement","icon":"mdi:home-lightning-bolt","enabled_default":true,"writeable":false},
{"register_name":"feed_in_power","name":"Feed-in Power","address":30358,"length":2,"platform":"sensor","poll_group":"fast","device_role":"emma","client_role":"emma","unit":"W","device_class":"power","state_class":"measurement","icon":"mdi:transmission-tower-export","enabled_default":true,"writeable":false},
{"register_name":"battery_charge_discharge_power","name":"Battery Charge/Discharge Power","address":30360,"length":2,"platform":"sensor","poll_group":"fast","device_role":"emma","client_role":"emma","unit":"W","device_class":"power","state_class":"measurement","icon":"mdi:battery-charging","enabled_default":true,"writeable":false},
{"register_name":"inverter_rated_power","name":"Inverter Rated Power","address":30362,"length":2,"platform":"sensor","poll_group":"slow","device_role":"inverter","client_role":"emma","unit":"W","device_class":"power","state_class":"measurement","icon":"mdi:solar-power-variant","enabled_default":true,"writeable":false},
{"register_name":"inverter_active_power","name":"Inverter Active Power","address":30364,"length":2,"platform":"sensor","poll_group":"fast","device_role":"inverter","client_role":"emma","unit":"W","device_class":"power","state_class":"measurement","icon":"mdi:solar-power","enabled_default":true,"writeable":false},
{"register_name":"state_of_capacity","name":"Battery State of Capacity","address":30368,"length":1,"platform":"sensor","poll_group":"fast","device_role":"emma","client_role":"emma","unit":"%","device_class":"battery","state_class":"measurement","icon":"mdi:battery","enabled_default":true,"writeable":false},
{"register_name":"ess_chargeable_capacity","name":"ESS Chargeable Capacity","address":30369,"length":2,"platform":"sensor","poll_group":"medium","device_role":"emma","client_role":"emma","unit":"kWh","device_class":"energy_storage","state_class":"measurement","icon":"mdi:battery-plus","enabled_default":true,"writeable":false},
{"register_name":"ess_dischargeable_capacity","name":"ESS Dischargeable Capacity","address":30371,"length":2,"platform":"sensor","poll_group":"medium","device_role":"emma","client_role":"emma","unit":"kWh","device_class":"energy_storage","state_class":"measurement","icon":"mdi:battery-minus","enabled_default":true,"writeable":false},
{"register_name":"backup_power_state_of_charge","name":"Backup Power State of Charge","address":30373,"length":1,"platform":"sensor","poll_group":"medium","device_role":"smartguard","client_role":"emma","unit":"%","device_class":"battery","state_class":"measurement","icon":"mdi:battery-heart","enabled_default":true,"writeable":false},
{"register_name":"emma_tou_periods","name":"EMMA TOU Periods","address":40004,"length":43,"platform":"sensor","poll_group":"medium","device_role":"emma","client_role":"emma","entity_category":"diagnostic","icon":"mdi:calendar-clock","enabled_default":false,"writeable":true,"format":"tou_periods"}
]})json";
  return JSON;
}

std::string HuaweiEmmaReverse::states_json_() {
  xSemaphoreTake(this->data_mutex_, portMAX_DELAY);
  std::ostringstream out;
  out << "{\"values\":{";
  bool first = true;
  for (size_t index = 0; index < CORE_NAMES.size(); index++) {
    if (!this->core_valid_[index]) continue;
    if (!first) out << ',';
    first = false;
    out << '\"' << CORE_NAMES[index] << "\":" << number_(this->core_values_[index]);
  }
  if (this->tou_valid_) {
    if (!first) out << ',';
    out << "\"emma_tou_periods\":" << this->tou_json_(this->tou_periods_);
  }
  out << "},\"updated_at\":{},\"unsupported\":[]}";
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
  if (!cJSON_IsArray(items) || cJSON_GetArraySize(items) > 14) {
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
  return "accessory";
}

uint32_t HuaweiEmmaReverse::u32_(uint16_t high, uint16_t low) { return static_cast<uint32_t>(high) << 16 | low; }
int32_t HuaweiEmmaReverse::i32_(uint16_t high, uint16_t low) { return static_cast<int32_t>(u32_(high, low)); }

std::string HuaweiEmmaReverse::number_(double value) {
  char buffer[32];
  if (std::fabs(value - std::round(value)) < 0.000001)
    std::snprintf(buffer, sizeof(buffer), "%.0f", value);
  else
    std::snprintf(buffer, sizeof(buffer), "%.3f", value);
  return buffer;
}

}  // namespace esphome::huawei_emma_reverse
