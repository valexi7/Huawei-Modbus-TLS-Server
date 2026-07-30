# ESPHome / LilyGO T-ETH-Elite roadmap

This is a design backlog, not an implementation commitment yet. The target is a LilyGO
T-ETH-Elite ESP32-S3 (16 MB flash, 8 MB PSRAM, W5500 Ethernet and PoE) that replaces the
Armbian/Python connector while preserving EMMA's unusual role arrangement: EMMA opens a
TLS socket as TCP client/Modbus slave, and the ESP32 accepts it while issuing Modbus
requests as master.

## Proposed architecture

1. Create an ESPHome external component named `huawei_emma_reverse` in a separate
   `esphome/components/huawei_emma_reverse` tree.
2. Use ESP-IDF on ESP32-S3 with ESPHome Wi-Fi plus the repository's `emma_w5500`
   compatibility component. Wi-Fi carries the Home Assistant native API and OTA; the
   fixed-address W5500 is isolated on the EMMA management network. Released ESPHome
   2026.7.3 still rejects its two built-in interfaces together, although upstream support
   has merged for a future release. The TLS listener must bind only to ETH1.
3. Implement a bounded single-client TLS 1.2 server using ESP-IDF/mbedTLS. Reject a
   second EMMA connection and apply handshake, frame-size, and request timeouts.
4. Port only the MBAP transport, Huawei `0x41` startup parsing, paged `0x2B` topology,
   grouped function-3 reads, and function-6/16 writes. Do not attempt to run the Python
   `huawei-solar` library on the microcontroller.
5. Generate a compact C++ register table from the pinned Python catalog at build time.
   The generator becomes the single mapping source for address, width, signedness,
   scale, enum, unit, poll group, device role, and writable range.
6. Expose ordinary measurements and controls through ESPHome's encrypted native API.
   Add custom native-API actions for complete TOU read/write because a 14-period
   structured schedule is not a natural scalar entity.
7. Keep Home Assistant schedule compatibility in a small companion integration only if
   BESS still requires the `growatt_server.*` aliases. Otherwise use ESPHome entities and
   API actions directly.

## Certificate strategy to prototype

- Generate a dedicated local CA and leaf key off-device with a repository script.
- Embed the leaf certificate/key in the firmware from ESPHome secrets or upload them to
  a protected filesystem partition; never expose the key as an entity or log value.
- Import only the CA certificate into EMMA.
- Evaluate renewal without changing the CA, secure-boot/flash-encryption support, and
  whether filesystem storage materially improves key handling over a compiled secret.
- Avoid on-device RSA key generation in the first version; it adds startup latency,
  entropy and persistence complexity without helping normal provisioning.

## Bring-up checklist when hardware arrives

- Confirm the exact T-ETH-Elite board revision and W5500 SPI CS, clock, MOSI, MISO,
  interrupt, and reset pins against LilyGO's official schematic/examples.
- Compile a minimal ESPHome 2026.7+ Ethernet node with DHCP, then a fixed management IP.
- Run a 24-hour PoE/Ethernet stability and reconnect test; record free/minimum heap.
- Prove an mbedTLS server accepts EMMA's cipher suite and captures the `0x41` frame.
- Replay the recorded startup/device-list exchange before enabling real polling.
- Port the six core power registers first and compare values against the Python server.
- Add grouped fast/medium/slow polling with watchdog-friendly yielding and strict buffer
  bounds (MBAP maximum, object length, register count, and TOU payload).
- Add writes only after readback verification; start with a harmless configuration item,
  then TOU in a controlled test window.
- Test link loss, EMMA restart, ESP32 restart, certificate failure, malformed frames,
  duplicate clients, request timeout, and OTA rollback.
- Measure flash, internal RAM, PSRAM use, task stack high-water marks, poll latency, and
  reconnect recovery before declaring the Armbian service replaceable.

## Repository status and remaining tasks

- [x] Confirm and package the LilyGO W5500 pins and dual-network base YAML.
- [x] Add a minimal local YAML that imports the maintained package from GitHub.
- Add the external-component C++ skeleton and code generator.
- Add captured-frame unit tests runnable on the host plus an ESP-IDF test target.
- Define ESPHome entity names/IDs and migration mapping from this HACS integration.
- Add OTA, API encryption, safe-mode, watchdog, status LED, and optional factory-reset
  button configuration.
- Write provisioning, certificate rotation, recovery, and rollback instructions.
- Decide whether the final repository ships one combined HACS/ESPHome project or splits
  firmware into its own versioned repository.

References: [ESPHome Ethernet](https://esphome.io/components/ethernet/),
[ESPHome external components](https://esphome.io/components/external_components/), and
[LilyGO T-ETH series](https://github.com/Xinyuan-LilyGO/LilyGO-T-ETH-Series).
