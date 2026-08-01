# Huawei EMMA external control API

The Home Assistant integration registers five actions intended for authenticated external
controllers:

- `huawei_emma.read_controls` discovers controls currently exposed as safe and writable.
- `huawei_emma.set_value` writes one exposed control and returns the value read back by
  the connector.
- `huawei_emma.set_tou_periods` replaces the TOU schedule from LUNA text or structured
  periods.
- `huawei_emma.read_time_segments` returns the nine-slot BESS-compatible schedule.
- `huawei_emma.update_time_segment` updates one BESS-compatible schedule slot.

These actions are available through Home Assistant automations and its REST action API.
They are separate from the connector's port `8088` API and use Home Assistant
authentication.

## Requirements and security

1. Install Huawei EMMA Management `0.11.0` or newer and restart Home Assistant.
2. Use a Home Assistant administrator's long-lived access token. An add-on can instead
   use its Supervisor token if the add-on has Home Assistant API access.
3. Keep Home Assistant behind HTTPS when requests cross an untrusted network.
4. Never put a token in a URL, log, source repository, or screenshot.

The API rejects non-administrator user tokens. It only exposes catalog entries already
represented as writable selects, switches, bounded numbers, datetimes, or the validated
TOU schedule. Read-only sensors and arbitrary Modbus addresses cannot be written.

Every successful write creates a Home Assistant log entry containing the register name
and EMMA serial number. The connector independently validates the type, enum, numeric
range, target device, and Modbus response.

For full request and validation tracing, enable:

```yaml
logger:
  logs:
    custom_components.huawei_emma_management: debug
```

DEBUG logging includes requested and normalized control values, register names, device
IDs, TOU schedules, translations, validation decisions, connector readback, and rejection
reasons. It deliberately excludes API tokens and authorization headers.

## Device ID

Use the Home Assistant Device Registry ID. Open the Huawei EMMA device page and copy the
last component of its URL:

```text
/config/devices/device/0123456789abcdef0123456789abcdef
                       └──────────── device_id ────────────┘
```

The ID can also be found in **Developer tools > Template**:

```jinja
{{ device_id('sensor.huawei_emma_pv_output_power') }}
```

## Discover writable controls

Use `return_response` because `read_controls` always returns data:

```bash
curl --request POST \
  --header "Authorization: Bearer $HA_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{"device_id":"0123456789abcdef0123456789abcdef"}' \
  'https://HOME_ASSISTANT/api/services/huawei_emma/read_controls?return_response'
```

Home Assistant wraps the result in `service_response`:

```json
{
  "changed_states": [],
  "service_response": {
    "device": {
      "model": "EMMA-A02",
      "serial_number": "TESTEMMA0001"
    },
    "accept_external_growatt_controls": true,
    "controls": [
      {
        "register_name": "storage_maximum_charging_power",
        "name": "Storage Maximum Charging Power",
        "platform": "number",
        "device_role": "inverter",
        "value": 10000,
        "available": true,
        "unit": "W",
        "minimum": 200,
        "maximum": 10000,
        "step": 1,
        "options": []
      }
    ]
  }
}
```

Use the returned `register_name` in write requests. Limits and options reflect the live
catalog and should be preferred over hard-coded values.

## Write a value

Add `return_response` to receive the normalized value returned by the connector:

```bash
curl --request POST \
  --header "Authorization: Bearer $HA_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "device_id": "0123456789abcdef0123456789abcdef",
    "register_name": "storage_maximum_charging_power",
    "value": 5000
  }' \
  'https://HOME_ASSISTANT/api/services/huawei_emma/set_value?return_response'
```

Example response:

```json
{
  "changed_states": [],
  "service_response": {
    "register_name": "storage_maximum_charging_power",
    "value": 5000
  }
}
```

Without `return_response`, the write still executes and Home Assistant returns its normal
list of changed states.

### Value types

| Control platform | JSON value |
| --- | --- |
| `number` | Number within the returned minimum/maximum and step |
| `switch` | `true` or `false` |
| `select` | Returned option key or human-readable label |
| `datetime` | Unix epoch seconds |
| `sensor` with `format: tou_periods` | Prefer `huawei_emma.set_tou_periods` |

## TOU schedule write

`huawei_emma.set_tou_periods` replaces the complete active schedule. Specify exactly one
of `structured_periods` or `periods`. Disabled/draft periods are not part of this
representation.

For Home Assistant action calls, `device_id` is optional when exactly one Huawei EMMA
entry is loaded; the integration resolves that sole coordinator automatically. **Fill
example data** inserts its actual registry ID. If multiple entries are loaded, choose a
device or supply `device_id` explicitly. Direct consumers should continue sending the
ID when they may run against multi-EMMA installations.

```bash
curl --request POST \
  --header "Authorization: Bearer $HA_TOKEN" \
  --header "Content-Type: application/json" \
  --data '{
    "device_id": "0123456789abcdef0123456789abcdef",
    "structured_periods": [
      {
        "start_time": "00:00",
        "end_time": "06:00",
        "charge_flag": "charge",
        "days_effective": [true, true, true, true, true, true, true]
      },
      {
        "start_time": "06:00",
        "end_time": "23:59",
        "charge_flag": "discharge",
        "days_effective": [true, true, true, true, true, true, true]
      }
    ]
  }' \
  'https://HOME_ASSISTANT/api/services/huawei_emma/set_tou_periods?return_response'
```

An empty list clears the schedule. EMMA accepts a maximum of 14 periods. Each enabled
period must have `start_time < end_time`, a `charge_flag`/`action` of `charge` or
`discharge`, and exactly seven `days_effective`/`days` Monday-to-Sunday booleans.

### LUNA text-format compatibility

Consumers using the LUNA text format can call the administrator-only
`huawei_emma.set_tou_periods` API:

```yaml
action: huawei_emma.set_tou_periods
data:
  device_id: 0123456789abcdef0123456789abcdef
  periods: |-
    00:00-03:59/1234567/+
    07:00-09:59/1234567/-
    17:00-20:59/1234567/-
```

Each line uses `HH:MM-HH:MM/DAYS/FLAG`. Weekdays are numbered `1` through `7`
(Monday-Sunday), `+` means charge, and `-` means discharge. Digits must be unique and in
ascending order; `1234567` selects every day. At most 14 non-empty periods are accepted.
Use a YAML block scalar (`|-`) as shown: spaces do not separate periods, and each period
must occupy its own line.
An empty string clears the schedule:

```yaml
action: huawei_emma.set_tou_periods
data:
  device_id: 0123456789abcdef0123456789abcdef
  periods: ""
```

The text is decoded before any connector call and then follows the same range, action,
weekday, Modbus-write, and readback validation as the structured API.

## Home Assistant action examples

Discover controls:

```yaml
action: huawei_emma.read_controls
data:
  device_id: 0123456789abcdef0123456789abcdef
response_variable: emma_controls
```

Set a switch or select:

```yaml
action: huawei_emma.set_value
data:
  device_id: 0123456789abcdef0123456789abcdef
  register_name: storage_charge_from_grid_function
  value: true
response_variable: emma_result
```

Read the BESS-compatible schedule:

```yaml
action: huawei_emma.read_time_segments
data:
  device_id: 0123456789abcdef0123456789abcdef
response_variable: emma_time_segments
```

Update one BESS-compatible slot:

```yaml
action: huawei_emma.update_time_segment
data:
  device_id: 0123456789abcdef0123456789abcdef
  segment_id: 1
  batt_mode: battery_first
  start_time: "00:00"
  end_time: "06:00"
  enabled: true
```

## Growatt/BESS emergency stop

The **Accept External Growatt Controls** option applies only to
`growatt_server.update_time_segment`. To disable BESS schedule writes:

1. Open **Settings > Devices & services**.
2. Find **Huawei EMMA Management**.
3. Select **Configure**.
4. Turn off **Accept External Growatt Controls** and submit.

Home Assistant reloads the config entry. Growatt schedule reads remain available, while
external Growatt update calls are rejected. Native Home Assistant controls and the
administrator-only `huawei_emma` API remain available for recovery.

## Errors

- `400`: malformed JSON/service data or a missing response query.
- `401`: missing or invalid Home Assistant token.
- Service validation error: token is not an administrator, a value/register is rejected,
  or Growatt controls are disabled.
- `500`: connector communication, Modbus write, or EMMA readback failed. Check the Home
  Assistant and connector logs before retrying.

Do not retry unsafe writes indefinitely. Read the controls again after an uncertain
response and compare the returned value before issuing another command.
