#pragma once

#include "esphome/components/binary_sensor/binary_sensor.h"
#include "esphome/components/output/binary_output.h"
#include "esphome/components/sensor/sensor.h"
#include "esphome/components/text_sensor/text_sensor.h"
#include "esphome/core/component.h"
#include "esphome/components/emma_w5500/emma_w5500.h"
#include "generated_register_catalog.h"

#include <esp_http_server.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/pk.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <string>
#include <ctime>
#include <vector>

struct cJSON;

namespace esphome::huawei_emma_reverse {

enum class ActivityKind : uint8_t { MODBUS_RX, MODBUS_TX, API_RX, API_TX };

// Stages of the EMMA session, reported by the EMMA Link Status entity.
enum class LinkStage : uint8_t {
  STARTING,
  WAITING_FOR_ADDRESS,
  TLS_SETUP_FAILED,
  LISTENING,
  HANDSHAKE,
  HANDSHAKE_FAILED,
  WAITING_FOR_STARTUP,
  DISCOVERING,
  POLLING,
  SESSION_ENDED,
};

struct ModbusFrame {
  uint16_t transaction{0};
  uint16_t protocol{0};
  uint8_t unit{0};
  std::vector<uint8_t> pdu;
};

struct TouPeriod {
  uint16_t start{0};
  uint16_t end{0};
  uint8_t charge_flag{0};
  uint8_t days{0x7F};
};

struct TopologyDevice {
  uint8_t object_id{0};
  uint8_t unit_id{0};
  std::string role;
  std::string model;
  std::string firmware;
  std::string serial;
  std::string protocol;
  std::string product_type;
};

struct CachedEntityValue {
  std::string json;
  uint32_t updated_ms{0};
  bool valid{false};
  bool subscribed{false};
  bool unsupported{false};
  bool not_applicable{false};
};

class HuaweiEmmaReverse : public Component {
 public:
  void set_ethernet(emma_w5500::EmmaW5500 *ethernet) { this->ethernet_ = ethernet; }
  void set_activity_output(output::BinaryOutput *output) { this->activity_output_ = output; }
  void set_connected_sensor(binary_sensor::BinarySensor *sensor) { this->connected_sensor_ = sensor; }
  void set_disconnects_sensor(sensor::Sensor *sensor) { this->disconnects_sensor_ = sensor; }
  void set_last_error_sensor(text_sensor::TextSensor *sensor) { this->last_error_sensor_ = sensor; }
  void set_link_status_sensor(text_sensor::TextSensor *sensor) { this->link_status_sensor_ = sensor; }
  void set_ports(uint16_t tls_port, uint16_t api_port) {
    this->tls_port_ = tls_port;
    this->api_port_ = api_port;
  }
  void set_api_token(const std::string &token) { this->api_token_ = token; }
  void set_tls_material(const std::string &certificate, const std::string &private_key) {
    this->certificate_ = certificate;
    this->private_key_ = private_key;
  }
  void set_log_raw(bool value) { this->log_raw_ = value; }
  // EMMA read-only mode: telemetry and discovery continue, but no Modbus
  // write function code is ever sent (external reserve provider in control).
  void set_read_only(bool value) { this->read_only_ = value; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

 protected:
  static void tls_task_entry_(void *parameter);
  void tls_task_();
  bool configure_tls_();
  bool accept_tls_client_();
  void close_tls_client_();
  bool read_exact_(uint8_t *data, size_t length);
  bool write_all_(const uint8_t *data, size_t length);
  bool read_frame_(ModbusFrame &frame);
  bool request_(uint8_t unit, const std::vector<uint8_t> &pdu, ModbusFrame &response);
  bool read_registers_(uint8_t unit, uint16_t address, uint16_t count, std::vector<uint16_t> &values);
  bool write_registers_(uint8_t unit, uint16_t address, const std::vector<uint16_t> &values);
  bool write_single_register_(uint8_t unit, uint16_t address, uint16_t value);
  void handle_unsolicited_(const ModbusFrame &frame);
  void parse_device_info_(const uint8_t *data, size_t length, TopologyDevice &device);
  void discover_topology_();
  void initialize_subscriptions_();
  void poll_group_(const char *group);
  void poll_entity_batch_(uint8_t unit, const std::vector<size_t> &indices, size_t begin, size_t end);
  bool decode_entity_(size_t index, const std::vector<uint16_t> &registers, size_t offset, std::string &json);
  std::string decode_structured_(const GeneratedEntityMetadata &metadata, const std::vector<uint16_t> &values);
  uint8_t unit_for_role_(const char *role, bool &available) const;
  size_t find_entity_(const std::string &register_name) const;
  bool write_entity_(size_t index, cJSON *value, std::string &readback, std::string &error);
  bool encode_scalar_(size_t index, cJSON *value, std::vector<uint16_t> &registers, std::string &error);
  bool write_tou_(size_t entity_index, const std::vector<TouPeriod> &periods, std::vector<TouPeriod> &readback);
  static std::vector<TouPeriod> decode_tou_(const std::vector<uint16_t> &registers);
  static std::vector<uint16_t> encode_tou_(const std::vector<TouPeriod> &periods);

  bool start_http_server_();
  static esp_err_t http_dispatch_(httpd_req_t *request);
  esp_err_t handle_http_(httpd_req_t *request);
  bool authenticate_(httpd_req_t *request);
  std::string read_http_body_(httpd_req_t *request);
  esp_err_t send_json_(httpd_req_t *request, const std::string &json, const char *status = "200 OK");
  esp_err_t send_entities_json_(httpd_req_t *request);
  std::string health_json_();
  std::string device_json_();
  std::string entities_json_();
  std::string states_json_();
  std::string tou_json_(const std::vector<TouPeriod> &periods);
  std::string diagnostics_json_();
  int8_t capability_state_(size_t capability_index) const;
  bool stale_(size_t index, uint32_t now) const;
  void apply_hardware_filters_();
  void expand_subscriptions_();
  static bool same_schedule_(const std::vector<TouPeriod> &left, const std::vector<TouPeriod> &right);
  bool parse_tou_json_(const std::string &body, std::vector<TouPeriod> &periods, std::string &error);

  void signal_activity_(ActivityKind kind);
  void update_activity_led_();
  void set_connected_(bool connected);
  void set_last_error_(const std::string &error);
  void publish_diagnostics_();
  void set_link_stage_(LinkStage stage);
  std::string link_status_text_() const;
  static std::string json_escape_(const std::string &value);
  static std::string role_for_(const std::string &model, const std::string &product);
  static uint32_t u32_(uint16_t high, uint16_t low);
  static int32_t i32_(uint16_t high, uint16_t low);
  static std::string number_(double value);
  static uint64_t u64_(const std::vector<uint16_t> &values, size_t offset);
  static std::string iso_time_(uint32_t updated_ms);

  emma_w5500::EmmaW5500 *ethernet_{nullptr};
  output::BinaryOutput *activity_output_{nullptr};
  binary_sensor::BinarySensor *connected_sensor_{nullptr};
  sensor::Sensor *disconnects_sensor_{nullptr};
  text_sensor::TextSensor *last_error_sensor_{nullptr};
  text_sensor::TextSensor *link_status_sensor_{nullptr};
  uint16_t tls_port_{GENERATED_DEFAULT_TLS_PORT};
  uint16_t api_port_{GENERATED_DEFAULT_API_PORT};
  std::string api_token_;
  std::string connector_instance_id_;
  std::string certificate_;
  std::string private_key_;
  bool log_raw_{false};
  bool read_only_{false};

  TaskHandle_t tls_task_handle_{nullptr};
  SemaphoreHandle_t tls_mutex_{nullptr};
  SemaphoreHandle_t data_mutex_{nullptr};
  httpd_handle_t http_server_{nullptr};
  std::atomic<bool> connected_{false};
  std::atomic<bool> subscriptions_changed_{false};
  std::atomic<uint8_t> pending_activity_{0};
  // Entity state is published from loop() on the main task only; the TLS task
  // and HTTP handlers just update these.
  std::atomic<uint32_t> disconnect_count_{0};
  std::atomic<uint32_t> error_sequence_{0};
  std::atomic<uint8_t> link_stage_{static_cast<uint8_t>(LinkStage::STARTING)};
  std::atomic<uint32_t> link_sequence_{0};
  std::atomic<uint32_t> peer_address_{0};
  std::atomic<int32_t> handshake_error_{0};
  uint32_t published_link_sequence_{UINT32_MAX};
  int8_t published_connected_{-1};
  int64_t published_disconnects_{-1};
  uint32_t published_error_sequence_{0};
  std::string last_fault_;
  uint32_t led_until_{0};
  uint8_t led_pulses_left_{0};
  bool led_on_{false};

  mbedtls_net_context listen_fd_;
  mbedtls_net_context client_fd_;
  mbedtls_ssl_context ssl_;
  mbedtls_ssl_config ssl_config_;
  mbedtls_x509_crt certificate_chain_;
  mbedtls_pk_context private_key_context_;
  mbedtls_entropy_context entropy_;
  mbedtls_ctr_drbg_context random_;
  bool tls_ready_{false};
  uint16_t next_transaction_{1};

  TopologyDevice emma_;
  std::vector<TopologyDevice> topology_;
  std::array<CachedEntityValue, GENERATED_ENTITY_COUNT> values_{};
  std::string last_error_;
  uint32_t reconnect_count_{0};
  uint32_t connected_at_ms_{0};
  uint32_t last_poll_ms_{0};
  std::string last_verified_write_;
  uint32_t last_verified_write_ms_{0};
};

}  // namespace esphome::huawei_emma_reverse
