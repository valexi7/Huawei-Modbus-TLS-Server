# ESPHome / LilyGO T-ETH-Elite roadmap

The target is a LilyGO
T-ETH-Elite ESP32-S3 (16 MB flash, 8 MB PSRAM, W5500 Ethernet and PoE) that replaces the
Armbian/Python connector while preserving EMMA's unusual role arrangement: EMMA opens a
TLS socket as TCP client/Modbus slave, and the ESP32 accepts it while issuing Modbus
requests as master.

## Implemented architecture

1. The ESPHome external component is maintained in
   `esphome/components/huawei_emma_reverse`.
2. Use ESP-IDF on ESP32-S3 with ESPHome Wi-Fi plus the repository's `emma_w5500`
   compatibility component. Wi-Fi carries the Home Assistant native API and OTA; the
   fixed-address W5500 is isolated on the EMMA management network. Released ESPHome
   2026.7.3 still rejects its two built-in interfaces together, although upstream support
   has merged for a future release. The TLS listener must bind only to ETH1.
3. A bounded single-client TLS 1.2 server uses ESP-IDF/mbedTLS, rejects a
   second EMMA connection and apply handshake, frame-size, and request timeouts.
4. The firmware implements MBAP transport, Huawei `0x41` startup parsing, paged `0x2B` topology,
   grouped function-3 reads, and function-6/16 writes. Do not attempt to run the Python
   `huawei-solar` library on the microcontroller.
5. A generated C++ register table comes from the pinned Python catalog.
   The generator becomes the single mapping source for address, width, signedness,
   scale, enum, unit, poll group, device role, and writable range.
6. The HACS integration remains the entity owner and consumes the same authenticated
   connector HTTP API as the Linux runtime. ESPHome's encrypted native API is retained
   for device management, logs, and OTA, avoiding duplicate sensor/entity ownership.
7. Home Assistant provides native `huawei_emma` controls and optional
   `growatt_server.*` compatibility independent of the selected connector runtime.

## Certificate strategy

- Generate a dedicated local CA and leaf key off-device with a repository script.
- Embed the leaf certificate/key in the firmware from ESPHome secrets or upload them to
  a protected filesystem partition; never expose the key as an entity or log value.
- Import only the CA certificate into EMMA.
- Evaluate secure-boot/flash-encryption support and
  whether filesystem storage materially improves key handling over a compiled secret.
- Avoid on-device RSA key generation in the first version; it adds startup latency,
  entropy and persistence complexity without helping normal provisioning.

## Production qualification checklist

- [x] Confirm the exact T-ETH-Elite board revision and W5500 SPI CS, clock, MOSI, MISO,
  interrupt, and reset pins against LilyGO's official schematic/examples.
- [x] Compile a dual-network ESPHome node and confirm the fixed W5500 management IP.
- Run a 24-hour PoE/Ethernet stability and reconnect test; record free/minimum heap.
- [x] Prove the mbedTLS server accepts the EMMA/mock cipher suite and parses `0x41`.
- [x] Replay startup/device-list, core telemetry, and TOU read/write/readback with the mock.
- [x] Add generated full-catalog fast/medium/slow polling with watchdog-friendly yielding and strict buffer
  bounds (MBAP maximum, object length, register count, and TOU payload).
- [x] Require readback verification for safe scalar and TOU writes.
- Test link loss, EMMA restart, ESP32 restart, certificate failure, malformed frames,
  duplicate clients, request timeout, and OTA rollback.
- Measure flash, internal RAM, PSRAM use, task stack high-water marks, poll latency, and
  reconnect recovery before declaring the Armbian service replaceable.

## Repository status and remaining tasks

- [x] Confirm and package the LilyGO W5500 pins and dual-network base YAML.
- [x] Add a minimal local YAML that imports the maintained package from GitHub.
- [x] Add the external-component C++ protocol/TLS/API implementation.
- [x] Compile the component against ESPHome/ESP-IDF 5.5 with bounded protocol buffers.
- [x] Generate the ESPHome C++ register table and complete entity migration manifest
  from the pinned Python catalog, with CI rejecting stale generated artifacts.
- [x] Add a networked mock for captured startup/topology, telemetry, TOU, reconnect, and
  error tests; a native ESP-IDF unit-test target remains optional.
- [x] Define stable firmware IDs and the migration policy from the HACS integration.
- [x] Add OTA, native API encryption, watchdog-compatible tasks, connection diagnostics,
  and a timing-coded activity LED.
- [x] Write initial provisioning and certificate-generation instructions.
- Complete certificate rotation, recovery, rollback, and real-device production soak-test guidance.
- Decide whether the final repository ships one combined HACS/ESPHome project or splits
  firmware into its own versioned repository.

References: [ESPHome Ethernet](https://esphome.io/components/ethernet/),
[ESPHome external components](https://esphome.io/components/external_components/), and
[LilyGO T-ETH series](https://github.com/Xinyuan-LilyGO/LilyGO-T-ETH-Series).
