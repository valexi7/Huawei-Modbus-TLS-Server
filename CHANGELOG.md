# Changelog

## 0.14.7

- Add `huawei_emma.read_value` with automatic single-entry device resolution, dynamic
  example device ID, and value/availability/unit/poll-timestamp response metadata.

## 0.14.6

- Synchronize connector polling subscriptions immediately when a Home Assistant entity
  is enabled or disabled, without requiring an integration reload.
- Log exact added/removed register names, active subscription totals per poll group, and
  ESP32 subscribed/requested/batch counts for each polling cycle.

## 0.14.5

- Add `huawei_emma.read_tou_periods` to return native EMMA TOU register data, its
  connector-poll timestamp, and device metadata without Growatt/BESS slot conversion.

## 0.14.4

- Include resolved Home Assistant device metadata and active EMMA TOU readback in the
  `huawei_emma.read_controls` response for straightforward action/API testing.

## 0.14.3

- Make **Fill example data** choose only the LUNA text TOU input, preventing its
  structured-period placeholder from violating the mutually exclusive input rule.
- Accept Home Assistant's flattened whitespace-separated LUNA example while retaining
  newline-separated YAML blocks as the canonical saved-automation format.

## 0.14.2

- Auto-fill the installation-specific EMMA device registry ID in Home Assistant action
  examples and allow it to be omitted when exactly one EMMA entry is loaded.
- Clarify that LUNA text schedules require a YAML multiline block with one period per
  line.

## 0.14.1

- Correct catalog acronym formatting so `Active` and `Accumulated` are no longer
  rendered as `ACtive` and `ACcumulated`.
- Rename Huawei's misleading `*_built_in_energy` and `*_external_energy` display-name
  families to **Built-in Meter ...** and **External Meter ...** while preserving stable
  register/entity IDs and the correct V, A, W, VA, power-factor, and kWh types.

## 0.14.0

- Promote the ESP32 connector from the core/TOU vertical slice to the complete generated
  740-register catalog with subscription-aware, topology-routed, grouped polling.
- Add generic scalar/string/timestamp/enum/bitfield and structured-period decoding,
  safe generic control writes with readback, unsupported-register isolation, and
  streamed catalog responses.
- Introduce a dependency-free connector contract as the single source for ports, poll
  intervals, protocol limits, and generated ESPHome defaults.
- Reorder and rewrite deployment documentation around embedded Home Assistant first,
  ESPHome second, and standalone Linux third.

## 0.13.1

- Add a networked Armbian EMMA mock that connects outbound to the ESP32 as a TLS
  client/Modbus slave and emulates startup, paged topology, changing core values, and
  stateful TOU read/write/readback.
- Add response-delay, forced-disconnect/reconnect, verified-CA, and raw-frame test modes.
- Log ESP32 connector API authentication outcomes and response metadata without exposing
  tokens.
- Document an isolated two-machine workflow where Armbian supplies mock Modbus data and
  a separate workstation pulls it from the ESP32 with `curl`.

## 0.13.0

- Generate the ESPHome firmware register table and a complete entity migration manifest
  from the same canonical Python/HACS catalog.
- Define stable firmware IDs and preserve HACS unique IDs by keeping the integration as
  entity owner when changing from the Python connector to ESPHome.
- Centralize metadata for the ten virtual TOU editor entities and enforce generated-file
  freshness in tests and GitHub validation.
- Drive ESP32 core decoding and `/api/v1/entities` metadata from the generated table.

## 0.12.0

- Add a compiled ESPHome/ESP-IDF reverse Modbus/TLS connector for the LilyGO
  T-ETH-Elite, retaining Wi-Fi for Home Assistant while binding EMMA to the isolated
  W5500 network.
- Implement Huawei startup and topology discovery, grouped core-value polling, and
  validated TOU schedule read/write/readback through the existing authenticated
  external-connector API.
- Drive the board's GPIO38 system LED with distinct timing patterns for Modbus RX/TX
  and API RX/TX activity.
- Add certificate/token provisioning tooling, firmware configuration, migration
  guidance, and an ESP32-S3 compile contract test.

## 0.11.0

- Consolidate all public Home Assistant actions under `huawei_emma`; remove the
  duplicate `huawei_emma_management` action surface while retaining that domain for
  installed integration config entries.
- Merge structured and LUNA-text schedule writes into
  `huawei_emma.set_tou_periods`, with mutually exclusive `structured_periods` and
  `periods` inputs.
- Add UI-mode descriptions, selectors, and examples for `read_controls`,
  `set_value`, `set_tou_periods`, `read_time_segments`, and
  `update_time_segment`.

## 0.10.1

- Add native Home Assistant action metadata for the dynamic
  `huawei_emma.set_tou_periods` API, including its device selector and multiline
  LUNA-format example.
- Return the verified EMMA schedule in both LUNA text and structured forms when
  action response data is requested.

## 0.10.0

- Add a LUNA-format-compatible `huawei_emma.set_tou_periods` service accepting
  newline-separated `HH:MM-HH:MM/DAYS/+|-` schedules while preserving the existing
  structured service.
- Parse the compatibility format into the same validated EMMA TOU write/readback path;
  an empty string clears the schedule.

## 0.9.1

- Preserve structured `emma_tou_periods` as a validated writable value for the
  `huawei_emma.set_value` service.
- Document the native config-entry service and generic device-ID service as distinct,
  complete Home Assistant action examples.

## 0.9.0

- Publish all 740 readable register definitions from the pinned `huawei-solar` catalog
  for EMMA, SUN2000/LUNA, SmartLogger, SDongle, and SCharger devices.
- Keep newly added catalog entities disabled by default and synchronize the enabled Home
  Assistant entity registry with the connector's active Modbus polling subscription.
- Route enabled registers to their discovered Modbus unit, skip absent optional hardware,
  and wake the scheduler immediately when subscriptions change.
- Classify entities into live sensors, safe configuration controls, and diagnostics;
  normalize units and expand device-specific icons and fast/medium/slow grouping.
- Preserve unknown upstream writeable registers as read-only diagnostics unless an
  explicit safe control schema is defined.

## 0.8.1

- Fix the Home Assistant configuration and options forms by using the serializable
  native port validator instead of a plain Python callback.
- Add AGPL-3.0 licensing and complete the repository metadata required by HACS.
- Correct manifest key ordering for Hassfest validation.

## 0.8.0

- Make the integration self-contained and ready for installation as a HACS custom
  repository.
- Add embedded Home Assistant reverse Modbus/TLS server mode with configurable port,
  managed local CA/certificate generation, and custom certificate/key support.
- Preserve external connector mode and suggest a random `EMMA_API_TOKEN` during setup.
- Migrate existing version-1 config entries to external mode without changing their
  connector credentials.
- Add stable embedded entity IDs and a one-time topology refresh after EMMA connects.
- Add HACS, Hassfest, unit-test, and Dependabot GitHub configuration.
- Add repository maintenance guidance and an ESPHome/LilyGO T-ETH-Elite roadmap.

## 0.7.1

- Add end-to-end debug logging for user controls, service/API validation, connector
  writes, readback, rejection, and completion.
