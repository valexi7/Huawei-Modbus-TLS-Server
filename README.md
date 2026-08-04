# Huawei EMMA for Home Assistant

Huawei EMMA Management exposes EMMA, SUN2000/LUNA, SmartGuard topology, SmartLogger,
SDongle, and SCharger data and controls as Home Assistant devices. It supports three
interchangeable connector runtimes, in this recommended order:

1. **Home Assistant embedded server** — simplest and recommended for most installations.
2. **ESPHome on LilyGO T-ETH-Elite** — isolated dual-network appliance with low power use.
3. **Standalone Python on Linux** — useful for development, diagnostics, and other hardware.

All three use the same Home Assistant integration, entity catalog, polling policy, safe
control schemas, and `/api/v1` connector contract. Changing runtime does not create new
Home Assistant entities: keep the existing config entry and change only its connector
mode or external host.

## Expanded documentation

- [Home Assistant integration and embedded TLS server](docs/home-assistant-embedded.md)
- [ESPHome / LilyGO T-ETH-Elite dual-network connector](docs/esphome-t-eth-elite.md)
- [Standalone Linux/Python connector](docs/linux-python.md)
- [Authenticated connector API and Home Assistant actions](docs/huawei-emma-api.md)
- [EMMA mock test workflow](docs/emma-mock-test.md)
- [Maintenance and upgrade guidance](docs/maintenance.md)
- [ESPHome architecture and production qualification roadmap](docs/esphome-roadmap.md)

EMMA uses a reverse connection: EMMA opens an outbound TLS socket as the TCP client, but
the connector becomes the Modbus master and EMMA answers as the Modbus slave. A normal
Modbus TCP server cannot handle its private `0x41` startup frame or this reversed role.

## Features

- Huawei private startup parsing and paged topology discovery
- 740 generated register definitions from pinned `huawei-solar` 3.0.6
- disabled-by-default optional entities; enabling an entity starts its polling
- address-coalesced fast (30 s), medium (5 min), and slow (30 min) polling
- automatic reapplication of Entity Registry-derived polling subscriptions after an
  ESP32 or Linux connector restart
- device-role routing for EMMA, inverter/LUNA, charger, SDongle, and SmartLogger
- scalar, enum, boolean, timestamp, bitfield, string, and structured-period decoding
- safe enum, boolean, datetime, bounded-number, and Huawei TOU writes with readback
- built-in meter enabled and normally absent external meter disabled by default
- Home Assistant TOU editor and `huawei_emma` actions
- Growatt scheduler compatibility with an emergency disable toggle
- local-only operation; no Huawei cloud dependency

## Install the Home Assistant integration

1. In **HACS > Integrations > Custom repositories**, add
   `https://github.com/valexi7/Huawei-Modbus-TLS-Server` as **Integration**.
2. Install **Huawei EMMA Management** and restart Home Assistant.
3. Open **Settings > Devices & services > Add integration > Huawei EMMA Management**.

The HACS integration owns every entity regardless of where the connector runs. This is
deliberate: automations, dashboards, device IDs, and entity-registry choices survive a
move from Home Assistant to ESP32 or Linux.

## Choice 1: Home Assistant embedded server (recommended)

Choose **Run inside Home Assistant**, retain TLS port `16100`, and enter the fixed IP or
DNS name that EMMA will use to reach Home Assistant. No separate machine, HTTP API token,
or `.env` file is required.

See the [embedded Home Assistant guide](docs/home-assistant-embedded.md) for certificate
paths, custom TLS material, migration, and troubleshooting.

### Automatic certificate

Choose **Automatic**. The integration creates a private local CA and server certificate
under:

```text
/config/huawei_emma_management/<config-entry-id>/certs/
```

Import only `ca-cert.pem` into EMMA's third-party management-system trust settings.
Never copy or expose `ca-key.pem` or `server-key.pem`. Certificate files live outside
the HACS directory and therefore survive integration updates.

Configure EMMA with:

- management server: the fixed Home Assistant IP/DNS name entered in the setup form
- port: `16100`
- protocol: Standard Modbus
- TLS: enabled
- trusted CA: the generated `ca-cert.pem`

Permit TCP `16100` between EMMA and Home Assistant, but do not expose it to the internet.

### Custom certificate

Choose **Custom** and provide certificate and private-key paths. Relative paths resolve
from `/config`; Home Assistant OS commonly exposes certificates under `/ssl`. The
integration checks validity and confirms the key matches before binding the listener.
The certificate SAN must contain the exact IP address or DNS name configured in EMMA.

## Choice 2: ESPHome on LilyGO T-ETH-Elite

This runtime keeps the networks physically separate:

```text
Home Assistant LAN <-- Wi-Fi --> ESP32-S3 <-- W5500/ETH1 --> EMMA LAN
       API / OTA                 :8088       TLS/Modbus :16100
```

The ESP32 is not a router or bridge. Wi-Fi carries ESPHome API, OTA, logs, and the
authenticated connector API. The W5500 accepts only EMMA's reverse TLS connection.

Copy [`esphome/huawei-emma-tls-server.yaml`](esphome/huawei-emma-tls-server.yaml) into
ESPHome Device Builder. It keeps local YAML small and loads the maintained package and
components from this repository. For production, replace `@main` with a release tag.

Add local deployment values to `secrets.yaml`:

```yaml
wifi_ssid: "HOME-ASSISTANT-WIFI"
wifi_password: "change-me"
huawei_emma_api_key: "BASE64-32-BYTE-ESPHOME-NATIVE-API-KEY"
huawei_emma_ota_password: "change-me"

emma_eth_ip: "192.168.88.20"
emma_eth_gateway: "192.168.88.1"
emma_connector_api_token: "GENERATE-A-RANDOM-TOKEN-OF-AT-LEAST-24-CHARS"

emma_tls_certificate: |-
  -----BEGIN CERTIFICATE-----
  ...
  -----END CERTIFICATE-----
emma_tls_private_key: |-
  -----BEGIN PRIVATE KEY-----
  ...
  -----END PRIVATE KEY-----
```

Generate a new CA/certificate from a repository checkout:

```bash
python tools/generate_esphome_tls.py --server-name 192.168.88.20
```

Merge the generated, git-ignored secrets snippet into ESPHome, and import only the
generated `ca-cert.pem` into EMMA. During migration you may instead embed the Linux
connector's existing `server-cert.pem` and `server-key.pem`; because EMMA already trusts
that CA, no trust change is needed.

Compile and flash once over USB, then verify:

1. Wi-Fi, native API, logging, and OTA work on the Home Assistant LAN.
2. W5500 has the fixed `emma_eth_ip` and answers on the EMMA LAN.
3. EMMA connects to W5500 port `16100` and the **EMMA TLS Connected** entity turns on.
4. The authenticated API answers on the ESP32 Wi-Fi address, port `8088`.

In the HACS integration choose **External connector** and enter the ESP32 Wi-Fi address,
port `8088`, and `emma_connector_api_token`. Configure EMMA with the W5500 address and
port `16100`—the two addresses are intentionally different.

The GPIO38 system LED uses timing because it is a binary LED: short pulse for Modbus RX,
medium for Modbus TX, double for API RX, and long for API TX.

Detailed provisioning, migration, and mock testing:

- [LilyGO T-ETH-Elite guide](docs/esphome-t-eth-elite.md)
- [Isolated Armbian EMMA mock test](docs/emma-mock-test.md)

## Choice 3: standalone Python on Linux

Python 3.12 or newer is required:

The [Linux/Python connector guide](docs/linux-python.md) covers first-run configuration,
systemd, upgrades, and troubleshooting in detail.

```bash
sudo systemctl disable --now huawei-emma.service 2>/dev/null || true
sudo git clone https://github.com/valexi7/Huawei-Modbus-TLS-Server /opt/huawei-emma
cd /opt/huawei-emma
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python modbus-server.py
```

The zero-argument start loads `.env`, generates a random `EMMA_API_TOKEN` if absent,
generates and validates a local CA/server certificate if needed, listens for EMMA on
`0.0.0.0:16100`, and starts the authenticated API on `0.0.0.0:8088`.

Read the generated token locally:

```bash
grep '^EMMA_API_TOKEN=' /opt/huawei-emma/.env
```

Choose **External connector** in Home Assistant and use the Linux host, port `8088`, and
that token. Import `/opt/huawei-emma/certs/ca-cert.pem` into EMMA and configure the Linux
host on port `16100`.

For automatic startup:

```bash
cd /opt/huawei-emma
sudo useradd --system --home-dir /opt/huawei-emma --shell /usr/sbin/nologin huawei-emma 2>/dev/null || true
sudo chown -R huawei-emma:huawei-emma /opt/huawei-emma
sudo cp deploy/huawei-emma.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now huawei-emma.service
sudo systemctl status huawei-emma.service
```

The files must actually be installed at `/opt/huawei-emma` because the supplied unit's
`WorkingDirectory`, `ExecStart`, and sandbox paths use it. Follow logs with:

```bash
journalctl -u huawei-emma.service -f
```

## Common entity and polling behavior

The integration initially enables the useful EMMA aggregate values, built-in energy
meter, identity data, and safe controls. The rest of the 740-register catalog is created
disabled. To use an optional value, open the device's **Entities** page, include disabled
entities, and enable it. On config-entry reload Home Assistant sends the enabled names as
the connector subscription.

Each runtime then:

- polls only subscribed registers;
- routes them to the discovered Modbus unit;
- combines adjacent addresses without exceeding 125 registers per request;
- polls live telemetry every 30 seconds, energy/configuration every 5 minutes, and static
  diagnostics every 30 minutes;
- isolates illegal-address/value failures and reports unsupported registers without
  repeatedly loading the bus.

The Home Assistant coordinator reads cached connector state every ten seconds; this does
not generate extra Modbus traffic. The raw `emma_tou_periods` value remains subscribed
because scheduling and the editor depend on it.

Enabling or disabling an entity updates connector subscriptions immediately. Home
Assistant logs the triggering entity ID and the accepted added/removed register names;
ESP32 and Python connectors log the same register changes and active totals for the
`fast`, `medium`, and `slow` groups. Poll logs distinguish the subscribed count from the
registers actually requested after topology and unsupported-register filtering.

## Home Assistant actions

Public actions are under `huawei_emma`; `huawei_emma_management` is only the internal
config-entry domain.

`device_id` may be omitted when exactly one Huawei EMMA integration entry is loaded.
The Actions editor's **Fill example data** command inserts that entry's actual device
registry ID automatically. With multiple EMMA entries, select the intended device in
the UI or provide its `device_id` explicitly.

Read all safe controls:

```yaml
action: huawei_emma.read_controls
data:
  device_id: abcd1234abcd1234abcd1234abcd1234
```

The response includes the resolved Home Assistant device ID, device name/model/serial,
and the active EMMA TOU schedule in both structured and LUNA text forms. This is useful
for validating automations without guessing which device a no-`device_id` action used.

Read one mapped value:

```yaml
action: huawei_emma.read_value
data:
  device_id: abcd1234abcd1234abcd1234abcd1234
  register_name: storage_maximum_charging_power
response_variable: emma_value
```

The response distinguishes a disabled entity (`subscribed: false` and
`availability_reason: not_subscribed_enable_the_entity`) from a subscribed register
that is still awaiting its first device poll. It also reports the poll group, target
device role, Modbus address, and register count. This action reads cached connector
state; enable the entity to start continuous polling.

When exactly one EMMA entry is loaded, `device_id` may be omitted. **Fill example data**
inserts the actual installation-specific device ID and the example register name.

Read only the native EMMA TOU register, without BESS/Growatt conversion:

```yaml
action: huawei_emma.read_tou_periods
data: {}
response_variable: emma_tou
```

The response includes `updated_at`, the time of the connector's most recent EMMA poll.

Write a safe mapped register:

```yaml
action: huawei_emma.set_value
data:
  device_id: abcd1234abcd1234abcd1234abcd1234
  register_name: storage_maximum_charging_power
  value: 8000
```

Write TOU using LUNA text:

```yaml
action: huawei_emma.set_tou_periods
data:
  device_id: abcd1234abcd1234abcd1234abcd1234
  periods: |-
    00:00-03:59/1234567/+
    07:00-09:59/1234567/-
```

The block scalar (`|-`) is the canonical form: every period is on its own line.
Home Assistant's current **Fill example data** YAML serializer flattens multiline text;
the integration also accepts its generated whitespace-separated form, but use `|-` in
saved automations for clarity.

Or structured periods:

```yaml
action: huawei_emma.set_tou_periods
data:
  device_id: abcd1234abcd1234abcd1234abcd1234
  structured_periods:
    - start_time: "00:00"
      end_time: "23:59"
      charge_flag: discharge
      days_effective: [true, true, true, true, true, true, true]
```

An empty text schedule clears all periods. Every schedule is validated for time range,
weekdays, maximum 14 periods, and overlap, then read back before success is returned.

See the [authenticated connector API guide](docs/huawei-emma-api.md) for direct HTTP use
and Growatt-compatible scheduling actions.

## One maintenance path

Runtime-neutral values live in:

- `custom_components/huawei_emma_management/connector_contract.py` — ports, limits,
  intervals, API version
- `custom_components/huawei_emma_management/embedded_catalog.py` — register metadata,
  ownership, categories, icons, safe controls
- pinned `huawei-solar==3.0.6` — register addresses, types, gains, and enum mappings

Generate the ESP32 tables and public migration manifest after changing any of them:

```bash
python tools/generate_esphome_catalog.py
```

This produces:

- `esphome/components/huawei_emma_reverse/generated_register_catalog.h`
- `esphome/components/huawei_emma_reverse/generated_contract.py`
- `esphome/entity-migration-map.json`
- `esphome/connector-contract.json`

CI runs the generator with `--check`; stale generated files fail validation. The ESP32
build therefore does not need Python or `huawei-solar`, while all three runtimes change
from the same source.

## Security and troubleshooting

- Never commit `.env`, ESPHome secrets, access tokens, CA keys, or server private keys.
- Replace any token pasted into chat, logs, screenshots, or a repository.
- The EMMA TLS listener and connector API are LAN services; do not port-forward them.
- A certificate must match the exact IP/DNS name configured in EMMA.
- `401 Unauthorized` means the Home Assistant external token and connector token differ.
- Enable `log_raw: true` on ESPHome or `--log-raw` on Linux for frame summaries; secrets
  are not logged.
- Debug logging records API validation and accepted/rejected controls without printing
  bearer tokens.

## Switching connector runtime

Home Assistant remains the entity owner. Switching the connector changes the coordinator
transport only; it does not recreate entities, dashboards, automations, or the EMMA
device ID.

### Linux ↔ ESPHome

Both use **External connector** mode. In **Settings > Devices & services > Huawei EMMA
Management > Configure**, replace the connector host, port, and API token, then reload
or restart the integration. Use the ESP32 **Wi-Fi** IP with port `8088` for ESPHome, or
the Linux host with port `8088` for the standalone connector.

Point FusionSolar/EMMA to the selected connector's TLS address on port `16100`: the
ESP32 **W5500/ETH1** IP for ESPHome, or the Linux host IP for standalone Python. Stop
the old external connector after the new one is receiving data so EMMA has one active
management endpoint.

### Home Assistant embedded ↔ external connector

Remove and add the integration again, selecting the desired mode during setup. This is
intentional: it prevents an accidental options edit from moving the Modbus/TLS control
endpoint. The integration preserves stable entity unique IDs when EMMA serial and
register names are unchanged; verify the discovered device and re-enable any optional
entities after migration.

Before changing FusionSolar, make sure EMMA trusts the CA for the new TLS listener and
that its certificate SAN contains the exact configured IP address or DNS name. Confirm a
successful TLS connection and API/state readback before retiring the old runtime.

## Development

Run repository tests and generated-file validation:

```bash
python -m unittest -v test_huawei_modbus_server.py
python tools/generate_esphome_catalog.py --check
```

The mock client can validate the ESP32 without Home Assistant or the real EMMA. See
[docs/emma-mock-test.md](docs/emma-mock-test.md).

Licensed under [AGPL-3.0](LICENSE).
