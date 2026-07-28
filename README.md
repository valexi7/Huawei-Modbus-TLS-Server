# Huawei EMMA reverse Modbus/TLS management system

This connector makes Huawei EMMA available as a real Home Assistant device, with
automatically discovered sensors, diagnostic entities, configuration controls, and a
service for time-of-use schedules.

EMMA uses an unusual connection arrangement:

- EMMA opens the outbound TLS connection, so it is the TCP client.
- This process accepts the socket, but acts as the Modbus master.
- EMMA remains the Modbus slave and answers requests on that socket.

That is why a normal `pymodbus` server rejected the captured private `0x41` startup frame.
The connector implements only the reverse socket/MBAP transport and delegates register
addresses, types, scaling, enums, batching, and writes to pinned
[`huawei-solar` 3.0.6](https://github.com/wlcrs/huawei-solar-lib/tree/v3.0.6).

## Install with HACS

The repository follows the HACS integration layout: every file required at runtime is
inside `custom_components/huawei_emma_management`. Until the repository is accepted as
a HACS default, add it as a custom repository:

1. Open **HACS > Integrations > three-dot menu > Custom repositories**.
2. Add `https://github.com/valexi7/Huawei-Modbus-TLS-Server` with category **Integration**.
3. Install **Huawei EMMA Management** and restart Home Assistant.
4. Open **Settings > Devices & services > Add integration > Huawei EMMA Management**.

Choose one deployment mode in the config flow:

- **embedded** (recommended): Home Assistant itself accepts EMMA's reverse TLS socket.
  No Raspberry Pi, Odroid, HTTP connector, `.env`, or API token is needed.
- **external**: Home Assistant connects to the standalone Python connector over its
  authenticated local HTTP API. This preserves the existing Armbian/Raspberry Pi setup.

The integration's **Configure** dialog can later change the active mode's port,
certificate settings, external address/key, and Growatt-control safety toggle. Switching
between embedded and external mode is intentionally done by removing and re-adding the
entry, so an accidental options edit cannot move the control endpoint.

HACS updates only the integration directory. Generated certificates live elsewhere
under `/config/huawei_emma_management`, so upgrades do not replace them.

## Embedded Home Assistant TLS server

Select `embedded`, choose the listener port (default `16100`), and enter the exact fixed
IP address or DNS name that EMMA will use for Home Assistant. Home Assistant starts and
stops the listener with the config entry.

### Automatically generated certificates

Leave **Certificate source** set to `automatic`. On first setup the integration creates:

```text
/config/huawei_emma_management/<config-entry-id>/certs/ca-cert.pem
/config/huawei_emma_management/<config-entry-id>/certs/ca-key.pem
/config/huawei_emma_management/<config-entry-id>/certs/server-cert.pem
/config/huawei_emma_management/<config-entry-id>/certs/server-key.pem
```

Copy `ca-cert.pem` to the commissioning device and import it as the trusted CA in EMMA's
third-party management-system settings. Never copy or expose either `*-key.pem` file.
The CA is valid for ten years and is retained while the leaf certificate is renewed.
Home Assistant logs the CA path and SHA-256 fingerprint, but never private-key content.

Configure EMMA with the Home Assistant host's fixed LAN IP/DNS name and the selected TLS
port. Permit that TCP port from EMMA through any host/VLAN firewall. Do not forward it
from the internet. If Home Assistant cannot bind the port, the integration remains in a
retry state and logs the exact address/permission/conflict error.

### Supplying your own certificate

Select `custom` and enter certificate and private-key paths. Relative paths resolve from
Home Assistant's `/config`; Home Assistant OS certificates commonly use absolute paths
such as `/ssl/fullchain.pem` and `/ssl/privkey.pem`. An encrypted RSA key may include its
password. The integration validates validity dates and verifies that the key matches the
certificate before it opens the listener.

EMMA must trust the CA that issued the custom server certificate, and the certificate's
subject alternative names must contain the exact IP address or DNS name entered in EMMA.
The integration reads custom files but never renews or overwrites them.

## External connector quick start

Python 3.12 or newer is required:

```bash
git clone YOUR_REPOSITORY_URL /opt/huawei-emma
cd /opt/huawei-emma
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python modbus-server.py
```

The zero-argument start does all safe first-run setup:

1. Loads `.env` automatically.
2. Creates `.env` and a random `EMMA_API_TOKEN` when the token is absent.
3. Validates the TLS certificate and private key.
4. Creates a persistent local CA and server certificate when no TLS files exist.
5. Listens for EMMA on `0.0.0.0:16100` and exposes the authenticated local API on
   `0.0.0.0:8088`.
6. Polls fast data every 30 seconds, cumulative/configuration data every 5 minutes,
   and identity/firmware data every 30 minutes.

Values in the project `.env` take precedence over stale variables exported by an earlier
interactive shell. At startup the API logs a 12-character SHA-256 token fingerprint (not
the token itself) so configuration mismatches can be diagnosed safely.

Secrets are never logged. Read the generated API token locally when configuring Home
Assistant:

```bash
grep '^EMMA_API_TOKEN=' .env
```

Do not commit `.env`, private keys, or access tokens. If a token has appeared in a chat,
terminal recording, or repository, replace it before continuing.

## Configure EMMA for an external connector

On first start, note the log line containing the generated CA path and SHA-256
fingerprint. In FusionSolar/EMMA commissioning, open **Settings > Communication
configuration > Third-party management system configuration** and configure:

- Connection: enabled
- Server: the connector host's fixed LAN IP address or DNS name
- Port: `16100`
- Network management protocol: Modbus / Standard Modbus
- TLS: enabled, TLS 1.2 or later
- Trust certificate: import `certs/ca-cert.pem`

If EMMA connects by a specific DNS name or IP, set `EMMA_SERVER_NAME` in `.env` before
the certificate is first generated:

```dotenv
EMMA_SERVER_NAME="emma-management.local"
```

The CA is retained across server-certificate renewals, so EMMA does not need to be
re-enrolled. Existing valid custom certificate/key pairs continue to work even when no
local CA files are present. To require mutual TLS, pass `--cafile CLIENT_CA.pem`.

The confirmed startup sequence for EMMA-A02 firmware `V100R025C00SPC115` is:

1. EMMA sends its private `0x41` startup/device description.
2. The connector pages through device-identification objects `0x87`, `0x88`, `0x89`, ….
3. It reads register `30000` (`MODEL_NAME`) and lets `huawei-solar` create the correct
   device implementation.
4. It begins grouped holding-register reads and exposes decoded values through the API.

The live response `2B 0E 03 03 FF 88 04 87 01 03` is paged: `04` is the reported total
object count, while only object `0x87` is in that packet. `FF 88` means “more follows;
request object 0x88.” This is why the earlier strict parser reported a truncated object.

## Configure Home Assistant for an external connector

If HACS is not being used, copy the integration directory into Home Assistant's
configuration directory:

```bash
cp -R custom_components/huawei_emma_management \
  /path/to/homeassistant/config/custom_components/
```

Restart Home Assistant, then go to **Settings > Devices & services > Add integration**,
select **Huawei EMMA Management**, and choose `external`. The form suggests a new random
API key. Before submitting the form:

1. Copy the suggested key exactly.
2. On the external connector, set it in `.env`:

   ```dotenv
   EMMA_API_TOKEN="paste-the-generated-key-here"
   ```

3. Restart the connector (`sudo systemctl restart huawei-emma.service`).
4. Submit the Home Assistant form with:

- Host: Raspberry Pi/Odroid address running the connector
- Port: `8088`
- API key: the same `EMMA_API_TOKEN` value

The key authenticates only Home Assistant-to-connector HTTP commands; it is unrelated to
the TLS certificate EMMA uses. If the external server already generated a secure token,
you may replace the suggested value with the existing `.env` value instead.

Home Assistant creates Huawei devices and entities from the connector catalog:

- Rapidly changing power, current, voltage, frequency, SOC, and power-factor sensors
- Cumulative yield, import/export, charge/discharge, and consumption sensors
- Firmware/model/serial and other diagnostic sensors
- Enum configuration registers as selects
- Boolean configuration registers as switches
- Bounded numeric configuration registers as number entities
- EMMA epoch values as native human-readable timestamp/datetime entities
- `EMMA_TOU_PERIODS` as a diagnostic schedule sensor plus the
  `huawei_emma_management.set_tou_periods` service
- Consecutively named active and planned schedule diagnostic sensors
- One period selector for slots 1-14, followed by shared Start Time, End Time,
  Charge/Discharge, and Enabled controls
- **Clear Period** and **Apply Schedule** buttons
- A writable diagnostic **TOU 10. Plan JSON** text entity for automations
- SUN2000/LUNA controls for maximum charge/discharge power, charge/discharge SOC limits,
  grid charging, working mode, forced charge/discharge, target mode, and PV power priority

The connector also parses every paged device description and creates subordinate Huawei
devices linked through EMMA. For example, the captured installation produces EMMA,
SmartGuard, and SUN2000 inverter devices.

### Optional register catalog and polling

The integration publishes all 740 readable definitions in the pinned `huawei-solar`
catalog: EMMA, SUN2000 inverter/LUNA, SmartLogger, SDongle, and SCharger register
families. The original core EMMA entities keep their existing defaults; every newly
added optional entity starts disabled. Open the device's **Entities** page, include
disabled entities, and enable only the measurements relevant to the installation.

Enabling an entity updates the connector's polling subscription on the config-entry
reload performed by Home Assistant. The register is then grouped by address and read at
its assigned interval:

- **fast (30 seconds):** live power, voltage, current, frequency, and similar telemetry
- **medium (5 minutes):** energy counters, operating state, configuration, schedules,
  and temperatures
- **slow (30 minutes):** identity, firmware, capabilities, and other static diagnostics

Disabling the entity removes it from the next connector subscription, so it no longer
uses Modbus bandwidth. The raw `EMMA_TOU_PERIODS` register is the sole exception: it is
kept internally subscribed because the TOU editor and scheduling services depend on it.
The Home Assistant coordinator still fetches the connector's cached state every ten
seconds; those requests do not cause additional Modbus reads.

Catalog entries are attached to the discovered device role and include the upstream
address, decoded unit, category, polling group, and an appropriate icon. If an enabled
register belongs to hardware that is not present, it remains unavailable and is not
repeatedly queried. Registers marked writeable upstream are exposed as controls only
when this project has an explicit safe enum, boolean, datetime, or numeric-range schema;
the remaining definitions are read-only diagnostic sensors.

`huawei-solar` 3.0.6 does not publish a SmartGuard-specific Modbus register table.
SmartGuard is therefore represented by its discovered model, serial number, firmware,
and topology relationship; the connector does not invent undocumented addresses.

Built-in EMMA meter entities are enabled by default. External-meter entities are disabled
by default and remain unavailable when the EMMA device list reports no external meter. On
upgrade, the integration also disables old external-meter registry entries that were
previously enabled by the integration; it does not override entities explicitly disabled
by the user. The integration fetches the connector's cached state every 10 seconds; that
does not cause extra Modbus reads.

Storage power and SOC controls use whole-watt and whole-percent steps, matching the
inverter register increments, so Home Assistant displays and writes them without decimal
fractions.

EMMA aggregate inverter measurements (including addresses 30302-30364) are read from
EMMA unit 0 and displayed under the inverter device. Native SUN2000 storage controls are
read and written through the inverter unit ID reported by the device list. Keeping device
ownership separate from Modbus routing prevents plausible-looking but invalid energy
values from being decoded from the wrong unit.

Example TOU service data:

```yaml
config_entry_id: "YOUR_CONFIG_ENTRY_ID"
periods:
  - start_time: "00:00"
    end_time: "06:00"
    action: charge
    days: [true, true, true, true, true, true, true]
  - start_time: "06:00"
    end_time: "23:59"
    action: discharge
    days: [true, true, true, true, true, true, true]
```

Huawei accepts at most 14 periods. The connector validates time order, action, weekday
count, enum values, boolean types, and numeric ranges before writing, then reads the
register back.

### BESS / Growatt time-segment compatibility

The integration exposes `huawei_emma_management.read_time_segments` and
`huawei_emma_management.update_time_segment` with the same request and response shape as
Home Assistant's Growatt time-segment actions. If no real Growatt integration or Growatt
actions are present, it also registers the exact aliases BESS expects:

- `growatt_server.read_time_segments`
- `growatt_server.update_time_segment`

Use the Home Assistant device ID of EMMA or any subordinate Huawei device owned by the
same integration entry. Reading returns nine fixed compatibility slots. Updating one slot
immediately writes the complete enabled schedule to EMMA, so no separate **Apply
Schedule** action is required:

```yaml
action: growatt_server.update_time_segment
data:
  device_id: a1b2c3d4e5f6
  segment_id: 1
  batt_mode: battery_first
  start_time: "00:00"
  end_time: "06:00"
  enabled: true
```

The mode translation is `battery_first` to EMMA `charge`, and `load_first` to EMMA
`discharge`. EMMA's TOU register has no separate grid-export priority, so `grid_first`
also maps to `discharge`; readback uses `load_first` unless that compatibility slot was
set as `grid_first` during the current connector session. Huawei supports 14 periods but
the Growatt contract exposes nine. Existing native periods 10-14 are preserved when a
compatibility slot is updated, while BESS controls slots 1-9.

If an actual Growatt integration is configured, the connector does not claim or replace
its `growatt_server` actions. Use the always-available
`huawei_emma_management.read_time_segments` and
`huawei_emma_management.update_time_segment` actions in that case.

To stop BESS from changing the schedule, open **Settings > Devices & services > Huawei
EMMA Management > Configure** and turn off **Accept External Growatt Controls**. The
`growatt_server.update_time_segment` action is then rejected for that entry. Schedule
reading and the native Huawei controls remain available. The option defaults to enabled
for compatibility with existing installations and reloads the entry when changed.

The integration also provides an authenticated, write-whitelisted Home Assistant action
API under the `huawei_emma` domain. External systems can discover the currently exposed
controls with `huawei_emma.read_controls` and write one with
`huawei_emma.set_value`. See the [Huawei EMMA external API guide](docs/huawei-emma-api.md)
for REST calls, response formats, value types, TOU examples, and security guidance.

### Control debug logging

Enable the integration logger to trace every control input and its path through schema,
device, whitelist/range/enum validation, mode translation, connector write, normalized
readback, and final refresh:

```yaml
logger:
  logs:
    custom_components.huawei_emma_management: debug
```

The trace covers number/select/switch/datetime entities, every TOU draft edit and apply,
Growatt/BESS reads and writes, the `huawei_emma` API, config-option changes, rejected
inputs, and connector error details. Lines use markers such as `CONTROL received`,
`validation=accepted`, `validation=rejected`, `TOU WRITE`, `GROWATT UPDATE`, and
`completed`. Tokens and authorization headers are never logged.

The TOU editor keeps changes local until **TOU 9. Apply Schedule** is pressed, so editing
a start or end time cannot write a temporarily incomplete period. Select a slot with
**TOU 3. Select Period**; the shared controls immediately display that slot's draft.
Disabled slots are left out of the submitted schedule. Newly enabled slots apply to all
seven days, while periods read from EMMA retain their existing weekday masks. **TOU 8.
Clear Period** resets only the selected draft slot. Applying a plan with no enabled slots
writes and verifies a valid zero-period schedule.

The active connector value and the planned draft remain structured lists internally;
neither is reconstructed by parsing the display text. Both diagnostic schedule sensors
expose `periods`, `schedule_lines`, and the complete multiline `schedule_text` as
attributes. The formatted sensor state itself is multiline while it fits Home Assistant's
255-character state limit; longer 1-14-period schedules use a short state and retain the
complete representation in those attributes.

`TOU 10. Plan JSON` uses a compact representation so every 14-period plan fits Home
Assistant's 255-character text-entity limit. Each item is
`[start_minutes,end_minutes,"c"|"d"]`. For example:

```json
[[0,270,"d"],[390,1439,"d"]]
```

Calling `text.set_value` validates the JSON and replaces only the local plan; it does not
write EMMA. An automation can then call `button.press` for **TOU 9. Apply Schedule**.
The text entity also accepts verbose objects with `start_time`, `end_time`, `action`, and
optional `days` fields when the JSON remains within the same Home Assistant limit.

Home Assistant 2026.3 and newer loads the integration logo and icon locally from the
integration's `brand/` directory. Both PNG assets are rendered from the provided
`custom_components/huawei_emma_management/brand/Huawei_Standard_logo.svg` source.
The HACS repository list can show its generic placeholder before the integration is
downloaded, because the local brand files do not exist in Home Assistant yet. After the
HACS download and a Home Assistant restart, the integration picker and integration page
use the bundled icon automatically; no manifest setting is required.

The older optional `HA_URL`/`HA_TOKEN` REST publisher remains available for compatibility,
but Home Assistant's REST state endpoint only creates state-machine representations. The
custom integration is what provides Device Registry ownership, unique IDs, entity
categories, controls, and availability. See Home Assistant's
[REST API](https://developers.home-assistant.io/docs/api/rest/) and
[Device Registry](https://developers.home-assistant.io/docs/device_registry_index/)
documentation. It is disabled unless `--legacy-ha-rest` or
`ENABLE_LEGACY_HA_REST=true` is set.

Once the custom integration is installed, remove or comment out `HA_URL` and `HA_TOKEN`
in the connector's `.env` to avoid a second set of legacy REST-created sensor states.
If legacy publishing is deliberately enabled over HTTPS, `HA_URL` must use a hostname
present in Home Assistant's certificate. Adding a CA with `HA_CA_FILE` establishes trust
but cannot fix an IP/hostname mismatch.

## API

Every endpoint requires `Authorization: Bearer $EMMA_API_TOKEN`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | Connection and poll health |
| GET | `/api/v1/device` | Model, serial, firmware, and topology |
| GET | `/api/v1/entities` | Complete entity/register metadata catalog |
| GET | `/api/v1/states` | Cached decoded values and update timestamps |
| POST | `/api/v1/subscriptions` | Replace the active poll set with `register_names` |
| POST | `/api/v1/entities/{register}/value` | Validated write to an exposed writable register |
| POST | `/api/v1/tou-periods` | Validated structured TOU schedule write |

Test locally:

```bash
set -a
. ./.env
set +a
curl -H "Authorization: Bearer $EMMA_API_TOKEN" \
  http://127.0.0.1:8088/api/v1/health
```

### Home Assistant cannot connect or authenticate

First test from the Home Assistant host/Terminal add-on, using the connector's numeric IP
if local DNS names are uncertain:

```bash
curl -v -H "Authorization: Bearer YOUR_TOKEN" \
  http://ODROID_IP:8088/api/v1/health
```

- If the connector logs nothing, the request did not reach it. Check that `odroid.lan`
  resolves inside Home Assistant, try the numeric IP, and check routing/firewall rules.
- If the connector logs `API authentication failed`, compare the startup
  `token_sha256=...` value with the local `.env` token fingerprint:

```bash
python3 -c "from dotenv import dotenv_values; import hashlib; t=dotenv_values('.env')['EMMA_API_TOKEN']; print(hashlib.sha256(t.encode()).hexdigest()[:12])"
```

The values must match. Quotes around the value in `.env` are parsed and are not part of
the token. Restart both the connector and Home Assistant after replacing the integration
files so the updated diagnostics and translations are loaded.

Raw arbitrary-address writes are deliberately not exposed. The original allowlisted
`/commands/...` routes remain for backward compatibility.

## Polling and batching

The register catalog is built at runtime from every readable definition in the installed,
pinned `huawei-solar` `REGISTERS` table. Only the active subscription is passed to
`HuaweiSolarDevice.batch_update()` when each poll group is due. That library combines
nearby definitions for the same Modbus unit into reads up to 64 registers with gaps below
16 registers. For example, the phase voltage/current/power definitions from 31639 through
31669 fit in one request instead of one request per entity.

If a firmware revision rejects a block, the connector recursively isolates the failing
definition for Modbus exception codes 2 or 3, marks that register unsupported for the
connection, and continues polling the rest. Raw frame logging is off by default; use
`--log-raw` only for diagnosis.

To diagnose an unavailable TOU schedule, stop the systemd service temporarily and run the
connector in the foreground from its installed directory:

```bash
sudo systemctl stop huawei-emma.service
cd /opt/huawei-emma
sudo -u huawei-emma .venv/bin/python modbus-server.py --log-raw
```

The focused trace identifies `emma_tou_periods`, unit ID, start address `40004`
(`0x9C44`), register count, the correlated response, and either the decoded period count
or the exact Modbus/decode failure. Restart the service afterward with
`sudo systemctl start huawei-emma.service`.

At startup, the connector publishes the fast, medium, and slow groups independently.
This prevents Home Assistant entities from remaining unknown while the initial full
catalog scan is still running. The API logs an `API state snapshot` whenever its
connected/value/unsupported counts change, making it clear whether Home Assistant has
received a populated cache.

Intervals can be overridden:

```bash
python modbus-server.py \
  --fast-interval 30 \
  --medium-interval 300 \
  --slow-interval 1800
```

## Start automatically with systemd

The repository includes [`deploy/huawei-emma.service`](deploy/huawei-emma.service). A
typical installation is:

```bash
# Run these commands from the repository directory.
cd ~/huawei-modbus-server

# Stop an earlier incomplete installation, if present.
sudo systemctl disable --now huawei-emma.service 2>/dev/null || true

# useradd does not create/populate /opt/huawei-emma for a system account.
id -u huawei-emma >/dev/null 2>&1 || \
  sudo useradd --system --home-dir /opt/huawei-emma \
    --shell /usr/sbin/nologin huawei-emma
sudo install -d -o huawei-emma -g huawei-emma -m 0750 /opt/huawei-emma

# Copy the application, including .env/certs, but rebuild the virtual environment.
sudo rsync -a \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  ./ /opt/huawei-emma/
sudo chown -R huawei-emma:huawei-emma /opt/huawei-emma

# Python 3.12 or newer is required.
python3 --version
sudo -u huawei-emma python3 -m venv /opt/huawei-emma/.venv
sudo -u huawei-emma /opt/huawei-emma/.venv/bin/python -m pip install \
  -r /opt/huawei-emma/requirements.txt
sudo -u huawei-emma /opt/huawei-emma/.venv/bin/python \
  /opt/huawei-emma/modbus-server.py --help

sudo install -o root -g root -m 0644 \
  /opt/huawei-emma/deploy/huawei-emma.service \
  /etc/systemd/system/huawei-emma.service
sudo systemctl daemon-reload
sudo systemctl enable --now huawei-emma.service
sudo systemctl status huawei-emma.service --no-pager
sudo journalctl -u huawei-emma.service -f
```

If `rsync` is not installed on Armbian, install it first with
`sudo apt update && sudo apt install rsync`, or copy the repository contents into
`/opt/huawei-emma` using another tool while preserving `.env` and `certs/`.

The service writes `.env` and generated certificates in `/opt/huawei-emma`; make that
directory writable only by `huawei-emma`. If a firewall is enabled, allow TCP `16100`
only from EMMA (for example `192.0.2.10`) and TCP `8088` only from Home Assistant.
Do not expose either listener to the internet.

After changing `.env`:

```bash
sudo systemctl restart huawei-emma.service
```

## Tests

```bash
.venv/bin/python -m unittest -v
```

The tests cover extended MBAP framing, request correlation, scaled writes, the captured
paged device-list response, multi-page collection, complete catalog generation, token
creation, certificate validation/renewal behavior, and managed embedded-listener
lifecycle.

Repository layout, release steps, CI checks, dependency updates, and the remaining
GitHub-owner assumption are documented in [Repository maintenance](docs/maintenance.md).
The deliberately deferred LilyGO T-ETH-Elite/ESPHome architecture and hardware bring-up
checklist are in [ESPHome roadmap](docs/esphome-roadmap.md).

This project and `huawei-solar` are licensed under AGPL-3.0; see [`LICENSE`](LICENSE).
Control writes can change grid and battery behavior; confirm local grid-code and
installer requirements before enabling automations.
