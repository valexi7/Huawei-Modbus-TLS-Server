# Supported hardware

The connector talks to EMMA only. EMMA forwards requests to the devices behind it
by Modbus unit ID, and the integration creates one Home Assistant device per
discovered role. Registers are routed to the device role that owns them. There is
no silent fallback to EMMA: a register whose device is not discovered is reported
as not applicable instead of being read from the wrong unit.

| Device | Role | Detected by | 0.15.0 status |
|---|---|---|---|
| EMMA-A02 (model name `SmartHEMS`) | `emma` | Startup frame and register 30000 | Supported since 0.14.x. Simulated in 0.15.0 tests. |
| SUN2000 inverter with LUNA2000 battery | `inverter` | Topology, `SUN2000` model | Supported since 0.14.x. Simulated in 0.15.0 tests. |
| SmartGuard | `smartguard` | Topology, then phase type, software version, and feature mask | **New** native device. Simulated only. |
| SCharger (`FusionCharge...`) | `charger` | Topology, model name prefix | Routing new in 0.15.0. Simulated only. |
| Smart AC gun | `smart_ac_gun` | Topology | Registers new in huawei-solar 3.2.4. Not simulated. |
| SDongle | `sdongle` | Topology | Unchanged from 0.14.x. Not simulated. |
| SmartLogger | `smartlogger` | Topology | Unchanged from 0.14.x. Not simulated. |

When several devices share a role, the first discovered one is used.

## SmartGuard

SmartGuard is its own device, enabled by default on new installs and on upgrade.
Entities you disabled yourself stay disabled.

- Single-phase SmartGuards hide phase B and C values as not applicable.
- Its status is polled fast; temperature medium; version, phase type, and feature
  mask slow.
- Four binary sensors interpret the SmartGuard status: **Grid Connected**,
  **Off-Grid Operation**, **Inverter Backup Supply**, and **Generator Supply**. A
  grid outage shows as off-grid or backup supply, not as a connector error. An
  unknown status value leaves them unknown instead of guessing.
- Grid switchover is detected as a capability only (SmartGuard feature mask bit
  0, and EMMA's manual off-grid switchover flag). The integration never writes the
  switchover register, in any mode.

## Connector runtimes

| Runtime | Tested in 0.15.0 |
|---|---|
| Home Assistant embedded server | HA test suite (HA 2026.10) and the simulated EMMA suite |
| Standalone Python (3.12, 3.13, 3.14) | Unit tests and the simulated EMMA suite |
| ESPHome on LilyGO T-ETH-Elite (ESP32-S3) | Compiles with ESPHome 2026.11.0, both normal and read-only. Not flashed. |

## Not verified on real hardware

No part of 0.15.0 was run against a real EMMA, LUNA, SmartGuard, or SCharger.
Before relying on it, check on a real system:

- discovery and unit IDs for SmartGuard and SCharger;
- the import-positive sign of `feed_in_power` and charge-positive sign of
  `battery_charge_discharge_power`;
- the corrected TOU weekday order (write a one-day schedule and compare it with
  FusionSolar);
- the new meter gains (voltage gain 10, current gain 100 on the external energy
  meter registers);
- read-only mode with a reserve provider connected;
- the ESPHome image on the LilyGO board, including free heap under load.
