# Using BESS Manager with Huawei EMMA

[BESS Manager](https://github.com/johanzander/bess-manager) can schedule a LUNA2000
battery behind EMMA through this integration. This page covers the current
manual setup, the sign conventions, SOC and backup reserve, and a proposed change
to BESS Manager's own discovery. This repository does not modify BESS Manager.

The analysis below was checked against BESS Manager commit `3f2e26e`
(`core/bess/huawei_controller.py`, `core/bess/ha_api_controller.py`,
`core/bess/settings_store.py`). Nothing here was tested with BESS Manager on real
hardware.

## How BESS Manager writes the schedule

BESS Manager's Huawei platform (`huawei_solar_luna2000`) does three things each
cycle:

1. Reads the TOU periods from the `Period N` attributes of a mapped sensor.
2. Compares them with its plan and writes the full list only when they differ.
3. Calls `<service_domain>.set_tou_periods` with `device_id` and a `periods` text
   such as `00:00-06:00/1234567/+`. It writes `00:00-00:14//+` (a period effective
   on no day) to clear the schedule.

This integration matches that contract:

- `huawei_emma.set_tou_periods` accepts the same text, including the
  `00:00-00:14//+` clear, and verifies the write by reading it back.
- The **EMMA TOU Periods** sensor (`emma_tou_periods`) publishes `Period 1` ...
  `Period N` attributes in the same text format. Inert no-day periods are left
  out, so a cleared schedule reads as zero periods. The sensor also has a
  `control_available` attribute that is `false` in read-only mode.
- The TOU sensor is enabled by default from 0.15.0. Upgrades re-enable it when it
  was disabled by the integration, never when you disabled it yourself.
- Day digits use the `huawei_solar` convention: `int(day) % 7`, so `7` and `0`
  are Sunday and `1234567` is every day. 0.14.x stored the EMMA weekday bitmask
  shifted by one day; 0.15.0 fixes that and stores Huawei's Sunday-first bit
  order.

## Manual setup today

In BESS Manager's inverter settings:

| Setting | Value |
|---|---|
| Platform | Huawei LUNA2000 (`huawei_solar_luna2000`) |
| `inverter.service_domain` | `huawei_emma` |
| Huawei device ID | The **EMMA** device of this integration |
| `inverter.grid_power_polarity` | `import_positive` when grid power is `feed_in_power` |
| Working mode entity | Leave unmapped. EMMA owns the mode. |

BESS Manager's automatic discovery only looks for the `huawei_solar` platform, so
map the sensors by hand:

| BESS key | Entity (register) | Device | Notes |
|---|---|---|---|
| `huawei_tou_periods` | EMMA TOU Periods (`emma_tou_periods`) | EMMA | Readback with `Period N` attributes |
| `battery_soc` | State Of Capacity (`state_of_capacity`) | EMMA | Plant SOC, % |
| `battery_charge_power` | Battery Charge Discharge Power (`battery_charge_discharge_power`) | EMMA | Charge-positive, W |
| `import_power` | Feed In Power (`feed_in_power`) | EMMA | Import-positive, W (see below) |
| `pv_power` | PV Output Power (`pv_output_power`) | EMMA | W |
| `local_load_power` | Load Power (`load_power`) | EMMA | W |
| `battery_charge_stop_soc` | Storage Charging Cutoff Capacity (`storage_charging_cutoff_capacity`) | Inverter | Writable number |
| `battery_discharge_stop_soc` | Storage Discharging Cutoff Capacity (`storage_discharging_cutoff_capacity`) | Inverter | Writable number |
| `grid_charge` | Storage Charge From Grid Function (`storage_charge_from_grid_function`) | Inverter | Writable switch |
| `lifetime_load_consumption` | Total Energy Consumption (`total_energy_consumption`) | EMMA | kWh |
| `lifetime_battery_charged` | Total Charged Energy (`total_charged_energy`) | EMMA | kWh |
| `lifetime_battery_discharged` | Total Discharged Energy (`total_discharged_energy`) | EMMA | kWh |
| `lifetime_import_from_grid` | Total Supply From Grid (`total_supply_from_grid`) | EMMA | kWh |
| `lifetime_export_to_grid` | Total Feed In To Grid (`total_feed_in_to_grid`) | EMMA | kWh |
| `lifetime_solar_energy` | Total PV Energy Yield (`total_pv_energy_yield`) | EMMA | kWh |

Unique IDs are `<EMMA serial>_<register name>`, the same shape as `huawei_solar`.

If an external reserve provider manages the battery, turn on
[read-only mode](read-only-mode.md). BESS Manager can still read every sensor, but
its `set_tou_periods` and SOC writes fail with a read-only error.

## Power sign conventions

The connector never inverts raw register values.

- `feed_in_power` on EMMA is **import-positive**: positive while importing from
  the grid. This matches the report in BESS Manager issue #818. BESS Manager's
  default for its Huawei platform is export-positive, because it assumes the
  separate SUN2000 power meter (`power_meter_active_power`). Set
  `inverter.grid_power_polarity: import_positive` when you map `feed_in_power`.
- `battery_charge_discharge_power` on EMMA is **charge-positive**, the same as
  LUNA register 37765. BESS Manager already assumes charge-positive.
- Optional derived sensors (disabled by default) remove the guesswork: **Grid
  Import Power**, **Grid Export Power**, **Battery Charging Power**, and **Battery
  Discharging Power** are never negative. Each one has `source_register`,
  `source_polarity`, and `derived: true` attributes.

Both conventions come from the register documentation and the BESS Manager issue.
They need confirmation on real hardware, for example by comparing the values with
FusionSolar during a known import.

## SOC limits and backup reserve

- The charge and discharge stop SOC (registers 47081 and 47082) are writable
  numbers on the inverter device. BESS Manager's `sync_soc_limits` writes them when
  they differ from its configuration. If EMMA or FusionSolar also manages these
  limits, they will fight; pick one owner.
- The backup reserve (`storage_backup_power_state_of_charge`, register 47102) is
  exposed as a **read-only** sensor. huawei-solar marks it writable, but it is
  not in the reviewed write allowlist. Set it in FusionSolar. BESS Manager should
  treat it as a floor: the battery keeps that much charge for backup supply, so
  discharge below it is not available to the schedule.
- EMMA's own peak-shaving SOC values are read-only too. See
  [peak shaving](peak-shaving.md).

## Proposed BESS Manager change

The manual setup works, but BESS Manager could discover EMMA by itself. This is a
proposal for the BESS Manager maintainers. It has not been sent upstream.

1. **Platform detection.** In `HAApiController._INVERTER_PLATFORMS`, add
   `huawei_emma_management` next to `huawei_solar` for `huawei`. It is the entity
   registry platform of this integration.
2. **Sensor map.** Add an `EMMA_SUFFIX_MAP` used for entities of platform
   `huawei_emma_management`, with the mapping from the table above. Keep
   `HUAWEI_SUFFIX_MAP` for `huawei_solar`. Map `emma_tou_periods` to
   `huawei_tou_periods`; it is enabled by default here, unlike in `huawei_solar`
   where the comment in `HUAWEI_SUFFIX_MAP` says it is left out for that reason.
3. **Service domain.** When the detected entities come from
   `huawei_emma_management`, default `inverter.service_domain` to `huawei_emma`
   instead of `huawei_solar`. The existing override still wins.
4. **Device ID.** Pick the device of the `emma_tou_periods` entity instead of the
   `huawei_solar` battery marker. `huawei_emma.set_tou_periods` also accepts no
   `device_id` when only one EMMA entry is loaded.
5. **Polarity.** When `import_power` maps to `feed_in_power` from
   `huawei_emma_management`, default `grid_power_polarity` to `import_positive`.
   `huawei_solar_luna2000` is already in `GRID_POWER_POLARITY_OVERRIDABLE`, so
   this is a default, not a new mechanism.
6. **Working mode.** Leave `huawei_working_mode` unmapped for EMMA. BESS Manager
   already skips the working-mode gate when it is unmapped and logs that this is
   expected behind EMMA.
7. **Read-only.** Before writing, check the `control_available` attribute of the
   TOU sensor. If it is `false`, skip the write and report that control is
   unavailable, instead of failing every cycle.

Steps 1 to 5 are enough for discovery; 6 needs no code; 7 is optional.
