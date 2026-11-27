# LilyGO T-ETH-Elite dual-network bring-up

The first firmware stage gives the ESP32-S3 two simultaneous interfaces:

- **Wi-Fi** connects to the Home Assistant network for native API, logs, and OTA.
- **ETH1/W5500** connects only to the EMMA management network using a fixed address.

This works with ESPHome **2026.4.0 or newer** through the repository's
`emma_w5500` external component. Released ESPHome 2026.7.3 still rejects a normal
configuration containing both `wifi:` and `ethernet:`. The upstream coexistence
change has merged for a future release, but using our dedicated component avoids
requiring an unreleased ESPHome build and gives the TLS listener an explicit ETH1.

The packaged W5500 pins come from LilyGO's official T-ETH-Elite definition:

| Signal | GPIO |
| --- | ---: |
| SCLK | 48 |
| MOSI | 21 |
| MISO | 47 |
| CS | 45 |
| Interrupt | 14 |
| Reset | not connected |

## Sample installation picture, when connected directly to EMMA router/network
<img width="600" height="700" alt="image" src="https://github.com/user-attachments/assets/3ab2aa65-0931-4815-8f99-071636552cf3" />

## Minimal device YAML

Copy [`esphome/huawei-emma-tls-server.yaml`](../esphome/huawei-emma-tls-server.yaml)
to the ESPHome configuration directory. It loads the maintained board package
directly from GitHub:

```yaml
packages:
  huawei_emma_board: github://valexi7/Huawei-Modbus-TLS-Server/esphome/packages/lilygo-t-eth-elite-dual-network.yaml@main

external_components:
  - source: github://valexi7/Huawei-Modbus-TLS-Server@main
    components: [emma_w5500, huawei_emma_reverse]
    refresh: 1h
```

Add these local values to `secrets.yaml`:

```yaml
wifi_ssid: "HOME-ASSISTANT-WIFI"
wifi_password: "change-me"
huawei_emma_api_key: "BASE64-32-BYTE-ESPHOME-API-KEY"
huawei_emma_ota_password: "change-me"
emma_eth_ip: "192.168.88.20"
emma_eth_gateway: "192.168.88.1"
```

Choose an unused fixed `emma_eth_ip` in the same subnet as EMMA. The example
assumes EMMA is on `192.168.88.0/24`. The gateway is required by ESPHome's
manual-IP schema; it does not need to provide internet access, and no forwarding
or bridge between Wi-Fi and ETH1 is enabled.

Pin a release tag instead of `@main` after firmware becomes production-critical.
Using `@main` is useful during bring-up because ESPHome refreshes the package and
receives repository fixes automatically.

## Validate before flashing

1. Update the ESPHome Device Builder add-on to 2026.4 or newer.
2. Validate the YAML, then install once over USB.
3. Confirm the Wi-Fi address appears in Home Assistant and OTA works.
4. Connect ETH1 to the EMMA network and confirm link and the fixed address in logs.
5. From a host on the EMMA network, ping the fixed ETH1 address.

Do not connect ETH1 to the Home Assistant LAN. This design deliberately keeps
the management networks separate. ESPHome's native and connector APIs may listen
on all local interfaces, but they are authenticated; the isolated EMMA network
should permit only the Modbus/TLS listener port from EMMA.

## Enable the ESP32 connector

The maintained example also loads `huawei_emma_reverse`. Add three sensitive values to
the ESPHome `secrets.yaml` file:

```yaml
emma_connector_api_token: "GENERATE-A-RANDOM-32-BYTE-TOKEN"
emma_tls_certificate: |-
  -----BEGIN CERTIFICATE-----
  ...
  -----END CERTIFICATE-----
emma_tls_private_key: |-
  -----BEGIN PRIVATE KEY-----
  ...
  -----END PRIVATE KEY-----
```

The simplest migration is to reuse the existing Python connector's `server-cert.pem`
and `server-key.pem`. EMMA already trusts their CA, so moving those two PEM values into
ESPHome secrets does not require replacing the trust certificate in EMMA.

For a new isolated CA, run from the repository root:

```bash
python tools/generate_esphome_tls.py --server-name 192.168.88.20
```

The script writes the CA/server files under `esphome/generated-certs`, creates a
git-ignored `esphome/emma-secrets.generated.yaml` snippet, and prints the CA fingerprint.
Merge that snippet into the Device Builder's `secrets.yaml` and import only
`ca-cert.pem` into EMMA. Do not commit the generated snippet, server key, or CA key.

The device YAML configures:

```yaml
huawei_emma_reverse:
  ethernet_id: emma_ethernet
  activity_output_id: led_output
  tls_port: 16100
  api_port: 8088
  api_token: !secret emma_connector_api_token
  certificate: !secret emma_tls_certificate
  private_key: !secret emma_tls_private_key
  read_only: false
```

Set `read_only: true` when an external reserve provider controls EMMA and the battery.
The firmware then refuses every Modbus write and answers control requests with
`423 Locked`; telemetry continues. See [read-only mode](read-only-mode.md).

Configure the Home Assistant integration in **external** mode using the ESP32 Wi-Fi IP,
port `8088`, and `emma_connector_api_token`. Configure EMMA's third-party management
address as the ESP32 W5500 IP on port `16100`.

## Production firmware scope

The connector provides:

- a single-client TLS 1.2 listener bound specifically to the W5500 address;
- Huawei `0x41` startup parsing and paged `0x2B` topology discovery;
- subscription-aware fast/medium/slow polling for the complete generated 915-entity
  catalog, with adjacent-address grouping and topology-unit routing;
- scalar, enum, mapping, boolean, bitfield, timestamp, string, and structured-period
  decoding;
- safe generic controls and structured `EMMA_TOU_PERIODS` validation, write, and
  readback;
- the existing authenticated `/api/v1` connector contract over Wi-Fi for Home
  Assistant external mode;
- strict shared MBAP/body/register/TOU limits, request serialization, reconnect handling,
  unsupported-register isolation, streamed catalog output, and optional raw-frame
  summaries;
- native ESPHome **EMMA TLS Connected**, **EMMA TLS Disconnects** (since boot) and
  **EMMA Last Error** diagnostic entities.

The firmware and Linux connector expose the same authenticated API contract. A new
firmware release should still pass the mock suite and at least a 24-hour real-device soak
test before unattended control is enabled.

## Reboot and outage diagnostics

The board package adds ESPHome diagnostic entities that show whether the ESP32 itself
restarted, for example a brownout during the grid to battery switchover of a power
outage:

| Entity | What it shows |
| --- | --- |
| Uptime | Seconds since boot; drops to 0 on every restart. |
| Last Boot | Boot time as a timestamp (needs Home Assistant time after boot). |
| Reset Reason | Why the chip restarted: power-on, brownout, watchdog, software/OTA, panic. |
| Boot Count | Boots stored in flash, so it also counts reboots while Home Assistant was down. |
| EMMA TLS Disconnects | EMMA sessions that ended since this boot. |
| EMMA Last Error | Most recent session error, for example `TLS read failed (-0x6800, EMMA silent for 5 s)`. |
| Restart | Button to restart the ESP32 and test recovery. |
| IP Address | Wi-Fi address used by Home Assistant, the connector API and OTA. |
| EMMA Network IP Address | W5500 address on the EMMA network. |

How to read an outage in the history:

- Boot Count went up and Reset Reason says brownout or power-on: the ESP32 lost
  power. Feed it from the backed-up circuit or add a small buffer (capacitor/UPS).
- Boot Count went up with a software reset and no OTA: check `wifi`/`api`
  `reboot_timeout`. ESPHome restarts after 15 minutes without Wi-Fi or without a
  Home Assistant API client, which can happen when the router or Home Assistant is
  not on backup power. Set `reboot_timeout: 0s` under `wifi:` and `api:` in your
  device YAML if the EMMA link should keep running in that case.
- Boot Count unchanged but EMMA TLS Disconnects went up: EMMA dropped or stalled
  the session; EMMA Last Error says how.

After a reboot the firmware listens again as soon as the W5500 has its address. EMMA
decides when it reconnects; the log line `EMMA TLS client connected Ns after boot`
shows the delay. On connect the firmware polls every subscribed register once, and
Home Assistant re-sends its subscriptions when it sees the new connector instance,
so entities return within one Home Assistant update (10 s) of that poll finishing.

## OTA upload fails with "Device closed connection without responding"

Firmware built before the socket fix declared none of the connector's sockets to
ESPHome, so the lwIP socket pool stayed at the default 10. With Home Assistant, EMMA
and the connector API all connected the pool is full, and a new OTA connection is
accepted and dropped at once. Current firmware reserves the connector's sockets, and
ESPHome raises the pool to about 15.

To get the fixed firmware onto a device that still runs the old one, free a few
sockets first:

1. In Home Assistant, disable the Huawei EMMA Management integration entry (Settings >
   Devices & services > the entry > Disable). This closes its connector API
   connections. Close any open ESPHome log windows for the device too.
2. Wait about 30 seconds, then start the OTA upload again.
3. Re-enable the integration entry after the device is back.

If that is not enough, restart or power-cycle the device and start the upload within
the first seconds after boot, before EMMA reconnects, or flash it over USB once.

## Entity IDs and generated mapping

Home Assistant remains the owner of measurement and control entities through the HACS
integration. ESPHome supplies the transport and register values rather than creating a
second set of native entities. Consequently, migrating from Python to ESPHome only
changes the integration's external connector host; existing entity-registry IDs and
automations remain intact.

The canonical logical ID is the Huawei register name. The generated mapping defines:

- HACS unique ID: `{emma_serial}_{register_name}`;
- firmware/ESPHome build ID: `emma_{register_name}`;
- display name, platform, device ownership, poll group, unit, classes, category, icon,
  address, length, and whether the current firmware supports the register.

[`esphome/entity-migration-map.json`](../esphome/entity-migration-map.json) contains all
915 physical catalog entities and 18 virtual entities (TOU editor, derived power,
and SmartGuard status). The generated C++
table includes every physical register; Home Assistant subscriptions determine which
ones use ESP32 RAM and Modbus bandwidth at runtime.

The tables and shared connector defaults are generated from the same
`embedded_catalog.py` and `connector_contract.py` used by the Python connector:

```bash
python tools/generate_esphome_catalog.py
```

Run this command whenever the Python catalog or the pinned `huawei-solar` version changes.
CI executes the generator with `--check` and rejects a commit if either generated file is
stale. ESPHome Device Builder then compiles the checked-in generated header, so it does
not need Python or `huawei-solar` on the ESP32 build host.

## Activity LED

LilyGO's official T-ETH-Elite definition exposes GPIO38 as a plain `LED_PIN`; it is not
an RGB/addressable LED. The connector therefore uses timing instead of color:

| Event | GPIO38 indication |
| --- | --- |
| Modbus RX | short pulse |
| Modbus TX | medium pulse |
| API RX | double pulse |
| API TX | long pulse |

The component keeps the activity event types separate internally so an RGB-capable
future board can map them to different colors without changing the protocol engine.

Before connecting the real EMMA, use the
[Armbian EMMA mock and separate curl test](emma-mock-test.md) to validate TLS,
topology discovery, register polling, TOU write/readback, authentication, and reconnect
behavior without sending mock measurements to Home Assistant.
