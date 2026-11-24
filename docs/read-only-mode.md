# EMMA read-only mode

Read-only mode is for installations where another system, such as an external
reserve provider or aggregator, manages the battery through EMMA. In this mode
the integration and connector keep reading EMMA, but they never send a Modbus
write. They do not try to override, interrupt, or interfere with the external
provider's battery management.

Read-only mode is off by default.

## What keeps working

- EMMA startup, topology discovery, and device creation, SmartGuard included.
- Telemetry polling with the usual fast, medium, and slow groups.
- Entity Registry-derived subscriptions, including the automatic restore after
  a connector restart. Subscriptions change only what is polled, so they are
  allowed.
- Control registers are still read where EMMA supports them. The current
  working mode, TOU periods, and SOC limits stay visible as entity states.
- `huawei_emma.read_controls`, `read_value`, `read_tou_periods`, and
  `growatt_server.read_time_segments`.
- Diagnostics and `/api/v1/health`.

## What is blocked

Every Modbus write function code (0x05, 0x06, 0x0F, 0x10, 0x15, 0x16, 0x17) is
refused at the request layer, before anything is sent to EMMA. That covers:

- TOU schedules (`huawei_emma.set_tou_periods`, the TOU editor, and
  `growatt_server.update_time_segment`);
- working mode, SOC limits, grid charging, and forced charge/discharge;
- peak shaving and capacity control;
- SmartGuard switching (also never written outside read-only mode);
- `huawei_emma.set_value` and every writable select, number, switch, datetime,
  and time entity.

In Home Assistant a blocked action raises a clear error: "EMMA read-only mode
is enabled: battery and EMMA control is disabled because an external provider
controls the system". The connector
API returns `423 Locked` for writes to `/api/v1/entities/{register_name}/value`,
`/api/v1/tou-periods`, and `/api/v1/commands/...`. The TOU editor entities show as unavailable. The other
control entities keep their state but reject changes, so dashboards and
automations still see the current values.

## Turning it on

Each runtime enforces read-only mode by itself. Home Assistant also refuses
writes when its own option is set or when the connector reports read-only.

| Runtime | Setting |
|---|---|
| Home Assistant embedded server | **Read-only mode** in setup or **Configure** (options) |
| Home Assistant with an external connector | **Read-only mode** in setup or options. This blocks writes from Home Assistant only. Set the connector option too if other clients use the connector API. |
| Standalone Python connector | `--read-only` or `EMMA_READ_ONLY=true` |
| ESPHome | `read_only: true` under `huawei_emma_reverse:` (compile-time) |

Changing the option reloads the config entry. It keeps the same entities,
unique IDs, and devices; nothing is recreated or renamed.

## How to check it

- The **Read-Only Mode** diagnostic binary sensor on the EMMA device. Its
  attributes show `integration_option`, `connector_reported`, and
  `control_available`.
- `/api/v1/health` and `/api/v1/diagnostics` report `read_only`;
  diagnostics also report `control_available` and `capabilities.writes`.
- The TOU periods sensor has a `control_available` attribute. BESS Manager and
  other schedulers can read it to see that schedule control is unavailable
  while telemetry is still valid.
- The Home Assistant config entry diagnostics download includes the same fields.

## Tests

- `tests/ha/test_integration.py`: rejected actions, option switching without
  entity recreation, and a connector that reports read-only.
- `tests/connector/test_read_only_api.py`: `423` responses, runtime switching,
  and an ESPHome source check.
- `tests/connector/test_simulated_emma.py`: discovery, polling, subscription
  restore, and zero writes reaching a simulated EMMA in read-only mode.

These are automated and simulated tests. Read-only mode has not been verified
against a real EMMA managed by a reserve provider.
