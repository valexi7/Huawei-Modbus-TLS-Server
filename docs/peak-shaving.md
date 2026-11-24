# Peak shaving and battery diagnostics

## Recommendation

Keep EMMA peak shaving **read-only** in 0.15.0, and keep configuring it in
FusionSolar. Do not add peak-shaving writes to the default write allowlist.
If they are added later, they belong in a separate pull request for experimental
energy controls, with a readback check and a real-hardware test plan.

## What is exposed

All of these are sensors, disabled by default and polled in the medium group
(5 minutes) once you enable them:

| Register | Address | Meaning |
|---|---|---|
| `emma_capacity_control_mode` | 40200 | `DISABLE`, `ACTIVE_CAPACITY_CONTROL`, or `APPARENT_POWER_LIMIT` |
| `emma_capacity_control_soc_peak_shaving` | 40201 | SOC kept back for peak shaving, % |
| `emma_capacity_control_periods` | 40202 (64 registers) | Peak-shaving periods and power limits |
| `emma_capacity_control_min_soc_peak_shaving` | 30700 | Lowest allowed peak-shaving SOC, % |
| `emma_capacity_control_max_soc_peak_shaving` | 30701 | Highest allowed peak-shaving SOC, % |

huawei-solar 3.2.4 marks 40200 to 40202 as writable. The integration reads them
only.

## Why not writable yet

- **It changes how much battery the schedule can use.** The peak-shaving SOC is
  held back from TOU and self-consumption discharge. A scheduler such as BESS
  Manager that does not know about it plans with capacity that EMMA will not
  release.
- **It depends on the grid contract.** The right limits come from the site's
  capacity tariff or connection agreement. A wrong value either fails to shave
  the peak or imports more than the contract allows.
- **The period block is structured and large.** Register 40202 is a 64-register
  block. A partial or misordered write would replace every period. The TOU writer
  has readback and idempotency checks; peak shaving would need the same, plus
  tests against a real EMMA.
- **It is untested on hardware.** The mock implements none of EMMA's peak-shaving
  behaviour, so simulated tests could not show a write is safe.
- **It conflicts with read-only mode** by design. Reserve providers often manage
  the same capacity.

Reading the values is still useful. A scheduler can subtract
`emma_capacity_control_soc_peak_shaving` from its usable capacity while
`emma_capacity_control_mode` is not `DISABLE`.

## Battery diagnostics

Battery pack diagnostics (per-pack SOH, temperatures, voltages, and the SOH
calibration status that is an enum in huawei-solar 3.2.4) stay **read-only**
sensors, disabled by default. Enable the ones you want to watch. None of them is
needed for scheduling.
