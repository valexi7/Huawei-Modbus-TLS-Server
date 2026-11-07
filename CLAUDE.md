# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A local-only connector between a **Huawei EMMA** energy manager and **Home Assistant**,
shipped as the HACS integration `huawei_emma_management`. It exposes EMMA plus its
discovered topology (SUN2000 inverters, LUNA batteries, SmartGuard, SmartLogger,
SDongle, SCharger) as HA devices with sensors and safe controls, including the Huawei
time-of-use (TOU) battery schedule and a Growatt-scheduler compatibility layer.

EMMA uses a **reverse connection**: in its "third-party management system" settings,
EMMA opens an outbound TCP socket (TLS by default) to us as the TCP *client*, but we
are the Modbus *master* and EMMA answers as the Modbus *slave*. EMMA first sends a
private Huawei function `0x41` startup frame (model, firmware, serial). A normal
Modbus TCP server cannot handle the reversed role or this frame, which is why this
project exists. Default ports: EMMA listener `16100`, connector HTTP API `8088`.

## Three runtimes, one integration

The HA integration always owns all entities. Only the "connector" (the thing EMMA
connects to) moves:

1. **Embedded** (recommended): the reverse listener runs inside HA
   (`ReverseModbusTlsServer` started from `async_setup_entry`). The coordinator talks to
   in-process state via `EmbeddedEmmaApiClient`.
2. **ESPHome on LilyGO T-ETH-Elite** (`esphome/`): ESP32-S3 with a W5500 on the EMMA LAN
   and Wi-Fi on the HA LAN. A C++ reimplementation of the protocol, polling, and HTTP
   API (`huawei_emma_reverse.cpp`, mbedTLS listener bound only to the W5500 address).
   The integration uses **External connector** mode over HTTP.
3. **Standalone Python on Linux**: `python modbus-server.py` runs the same
   `embedded_server.py` as a CLI process plus the HTTP `CommandApi`. Also external mode.

All three share one contract: same entity catalog, poll groups, write validation, and
`/api/v1` endpoints. Switching runtime must never change entity unique IDs.

## Commands

The CI uses Python 3.13; the standalone runtime needs Python 3.12 or newer. The tests
need only `requirements.txt` (no Home Assistant install).

```bash
python -m pip install -r requirements.txt
python -m unittest -v                                   # full suite (what CI runs)
python -m unittest test_huawei_modbus_server.FrameTests.test_master_correlates_read_response   # single test
python tools/generate_esphome_catalog.py --check        # CI fails on stale generated files
python tools/generate_esphome_catalog.py                # regenerate ESPHome tables/manifests
python -m compileall -q custom_components huawei_modbus_server.py
python -m json.tool custom_components/huawei_emma_management/manifest.json
```

On Windows the interpreter may be `py`, not `python`.

Manual and protocol testing without real hardware: `tools/emma_mock_client.py` plays EMMA
(it sends the `0x41` startup, answers topology pages, registers, and TOU writes). It
supports `--ca-file`, `--insecure`, or `--no-tls`. See `docs/emma-mock-test.md`.

CI (`.github/workflows/validate.yml`) runs HACS validation, Hassfest, the generator
`--check`, and the unit tests on every push, on pull requests, and daily.

## Source layout and the "one maintenance path"

- `custom_components/huawei_emma_management/` is the **canonical code** and the complete
  HACS artifact. The root files `huawei_modbus_server.py`, `modbus-server.py`,
  `emma_catalog.py`, and `runtime_setup.py` are compatibility shims. They append the
  component directory to `sys.path` and re-export it. Never put logic in them.
- `embedded_server.py`, `embedded_catalog.py`, `embedded_runtime_setup.py`, and
  `connector_contract.py` must stay importable **without Home Assistant**, because the
  standalone server and the tests use them. Each one uses `try: from .x import …` /
  `except ImportError: from x import …`. Keep that pattern and don't import
  `homeassistant` there.
- Runtime-neutral sources of truth:
  - `connector_contract.py`: ports, API prefix/paths, Modbus limits (125 read and 123
    write registers, max MBAP length), poll intervals (fast 30 s, medium 300 s, slow
    1800 s), 14 TOU periods, contract version.
  - `embedded_catalog.py`: builds the ~740-entry `EntityDescription` catalog from the
    pinned `huawei-solar==3.0.6` `REGISTERS`. It derives platform, poll group, entity
    category, default enablement, HA device/state class, icon, friendly name, and
    target device role.
  - The `huawei-solar` pin supplies register addresses, types, gains, and enums. Keep it
    pinned and change it only after hardware testing.
- After changing any of these, run `tools/generate_esphome_catalog.py`. It writes the
  four generated files: `esphome/components/huawei_emma_reverse/generated_register_catalog.h`,
  `generated_contract.py`, `esphome/entity-migration-map.json`, and
  `esphome/connector-contract.json`. Commit them together with the source change, and
  don't hand-edit them. Protocol or behaviour changes in Python usually need a matching
  change in `huawei_emma_reverse.cpp`. `RepositoryContractTests` checks parts of this
  parity.

## Connector internals (`embedded_server.py`)

- **Framing**: `read_modbus_frame` and `ModbusFrame` handle MBAP with Huawei's extended
  length. `ReverseModbusSession.run()` reads every inbound frame. `_dispatch` resolves
  pending requests by transaction ID; an unsolicited `0x41` frame sets the device info
  and the startup event.
- **Master side**: `ReverseModbusSession._master_loop` waits for startup, then discovers
  topology. `read_device_list` pages through device identification objects;
  `parse_topology_devices` assigns roles such as `emma`, inverter/LUNA, charger,
  sdongle, and smartlogger. It then polls the subscribed registers by group. Reads go
  through `huawei-solar`'s `AsyncHuaweiSolarClient` on a custom tmodbus transport
  (`ReverseConnectionTransport`) that sends over the EMMA-initiated socket. There is
  one client per device role and unit ID.
- **Polling policy**: only subscribed registers are polled. Adjacent addresses are
  coalesced up to 125 registers per request. Illegal-address/value errors mark a
  register as unsupported instead of retrying it. Values are decoded into JSON-safe
  state.
- **Writes**: `set_register_value` and `_convert_register_value` validate enums, bounds,
  and target role, scale the value with the library's gain, write it, and **read it
  back** before reporting success. `set_tou_periods` validates and writes the structured
  TOU register.
- **`RuntimeState`** is the process-wide cache shared by the session and the API. It
  holds the active session, latest values, subscriptions, health (including a
  per-process `connector_instance_id`), device, and entities. It allows **one active EMMA
  session**; additional connections are rejected.
- **`ReverseModbusTlsServer` / `ServerConfig`** form the lifecycle wrapper used by HA.
  `use_tls=False` serves plain TCP and skips certificate handling.
  `embedded_runtime_setup.ensure_certificates` creates or validates a persistent local
  CA and an RSA server certificate whose SAN must match the address configured in EMMA.
- **`CommandApi`** is a minimal hand-written HTTP/1.1 server for external mode. **Every**
  request needs `Authorization: Bearer <token>` (compared with `hmac`).
  - Under `/api/v1`: `GET health|device|entities|states`, `POST subscriptions`,
    `POST entities/{register_name}/value`, and `POST tou-periods`.
  - Legacy routes: `GET /health`, which adds startup info, and `POST /commands/{name}`.
  - Operator-facing usage is documented in `docs/huawei-emma-api.md`.
- **Standalone CLI**: `parse_args` and `async_main`. `main()` loads `.env` and
  auto-creates `EMMA_API_TOKEN`. Options: `--no-tls` / `EMMA_USE_TLS=false`,
  `--log-raw`, `--once`, the poll interval flags, and the optional legacy HA REST
  publisher (`--legacy-ha-rest`).

## Home Assistant side

- `config_flow.py`: config entry `VERSION = 2`. Mode is `embedded` (port, Use TLS,
  automatic or custom certificate) or `external` (host, port, token). The options flow
  edits the same fields and `accept_external_growatt_controls`. Moving between embedded
  and external mode is deliberately **not** possible via options; you re-add the entry.
- `__init__.py`:
  - `async_setup_entry` builds the API client (embedded or HTTP), the coordinator, and
    the entity-registry cleanup and migration.
  - It listens to entity-registry updates so that enabling or disabling an entity
    immediately changes the connector subscriptions.
  - It registers the services: public `huawei_emma.*` (`read_controls`, `read_value`,
    `set_value`, `set_tou_periods`, `read_tou_periods`, `read_time_segments`,
    `update_time_segment`) and optional `growatt_server.*` aliases.
  - `huawei_emma_management` is only the config-entry domain; public actions live
    under `huawei_emma`.
  - `device_id` may be omitted when exactly one entry is loaded.
- `api.py`: `EmmaApiClient` (aiohttp, external mode) and `EmbeddedEmmaApiClient`
  (in-process) share one interface: `health`, `device`, `entities`, `states`,
  `set_subscriptions`, `set_value`, `set_tou_periods`.
- `coordinator.py` (`EmmaCoordinator`) polls the **connector's cached state** every ~10 s.
  This adds no Modbus traffic.
  - `async_sync_polling_subscriptions` derives the poll set from the Entity Registry:
    enabled entities, plus defaults for unregistered ones, plus the always-required
    `emma_tou_periods`.
  - `_async_reconcile_connector_subscriptions` reasserts that set when the connector's
    instance ID changes (restart) or its subscription count differs.
  - It also holds the TOU editor slots and the 9-slot Growatt view.
- `tou.py`: conversions between LUNA text (`HH:MM-HH:MM/1234567/+|-`), structured
  periods, compact editor JSON, and Growatt modes (`load_first`, `battery_first`,
  `grid_first`).
- Platform modules (`sensor`, `select`, `switch`, `number`, `datetime`, `time`, `text`,
  `button`) build entities from catalog descriptions via `entity.EmmaEntity`. Unique ID
  format: `{unique_id_prefix}_{register_name}`. Devices are grouped by topology role.
- User-facing strings live in `strings.json` **and** `translations/en.json`. Keep both
  in sync.
- Hassfest validates `manifest.json`. Its `requirements` (what HA installs:
  `huawei-solar==3.0.6`, `tmodbus>=0.6.2`) are separate from `requirements.txt` (used by
  standalone and CI, currently `tmodbus==0.4.1`). When changing dependencies, update
  and check both.

## Conventions

- Releases: bump `manifest.json` `version`, add a `CHANGELOG.md` entry, and tag
  `v<version>` with a GitHub Release (HACS uses releases). Recent commits are named
  `Release X.Y.Z <summary>`.
- Never commit `.env`, PEM files, tokens, real LAN addresses, device serials, or
  captured customer data. Docs and tests use synthetic device IDs (`abcd1234…`), and a
  test enforces this.
- Keep debug logging free of bearer tokens and keys. Log register names and accepted or
  rejected decisions instead.
- The working tree uses CRLF on Windows (`core.autocrlf=true`); the index is LF.
- More guidance: `docs/maintenance.md` (release checklist), `docs/esphome-roadmap.md`
  (ESP32 design constraints), `docs/home-assistant-embedded.md`, `docs/linux-python.md`.
