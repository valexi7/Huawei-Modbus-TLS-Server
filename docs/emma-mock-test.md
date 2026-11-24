# Testing the ESP32 with the Armbian EMMA mock

This test keeps the Modbus/TLS side and connector-API side independent:

```text
Armbian EMMA mock                         Separate test workstation
TLS client / Modbus slave                curl / HTTP client
192.168.88.30                                    |
        |                                        | Home Assistant LAN/Wi-Fi
        v                                        v
ESP32 W5500 192.168.88.20:16100        ESP32 Wi-Fi 192.168.1.194:8088
TLS server / Modbus master              authenticated connector API
```

The mock does not call the HTTP API and does not require
`emma_connector_api_token`. The separate workstation uses the token only when pulling
the ESP32's decoded mock values. The ESP32 does not push those values to Home Assistant.

## 1. Isolate the production systems

- Do not configure the real EMMA to connect to the ESP32 during this test. The firmware
  currently accepts one Modbus/TLS client.
- Stop the Python connector on Armbian if it is still running, so its listener logs are
  not confused with the mock-client logs:

  ```bash
  sudo systemctl stop huawei-emma.service
  ```

- Disable the HACS config entry that points to the ESP32, or leave it configured with a
  deliberately different token. The latter produces repeated `401` requests; disabling
  the entry is quieter.
- The native ESPHome integration can remain connected. It exposes the TLS connection
  diagnostic, not the mock Modbus measurements.

## 2. Prepare Armbian

Give Armbian an unused address on the isolated test network, such as
`192.168.88.30/24`, and verify that it can reach the W5500 interface:

```bash
ping 192.168.88.20
```

Clone or update the repository and create a small virtual environment:

```bash
git clone https://github.com/valexi7/Huawei-Modbus-TLS-Server.git
cd Huawei-Modbus-TLS-Server
python3 -m venv .mock-venv
source .mock-venv/bin/activate
```

The mock uses only the Python standard library; no package installation is required.

## 3. Start the mock EMMA

For a verified TLS connection, copy the CA certificate that signed the certificate
compiled into the ESP32 and run:

```bash
python3 tools/emma_mock_client.py \
  --host 192.168.88.20 \
  --port 16100 \
  --local-address 192.168.88.30 \
  --ca-file /path/to/ca-cert.pem
```

The ESP32 certificate must contain `192.168.88.20` in its subject alternative names.
For an isolated first connection test only, certificate verification can be disabled:

```bash
python3 tools/emma_mock_client.py \
  --host 192.168.88.20 \
  --local-address 192.168.88.30 \
  --insecure
```

Add `--log-raw` to print complete transmitted and received frames. The mock:

- sends the captured-shape Huawei private `0x41` startup frame (EMMA-A02);
- returns paged device-identification responses for EMMA (unit 0, model name
  `SmartHEMS` at register 30000 like a real EMMA), SmartGuard (unit 2), SUN2000/LUNA
  (unit 3), and optionally an SCharger (unit 4);
- answers each unit from its own register bank, so routing mistakes show up as
  wrong or missing values instead of silently reading EMMA registers;
- returns slowly changing PV, load, feed-in, battery, inverter, and SOC values;
- exposes a two-period TOU schedule;
- validates and retains TOU writes so the ESP32 can read them back;
- rejects unknown register ranges with Modbus `Illegal Data Address`;
- reconnects automatically after a connection failure.

Useful fault tests are:

```bash
# Delay every Modbus response by 750 ms.
python3 tools/emma_mock_client.py --host 192.168.88.20 --insecure --delay-ms 750

# Disconnect after ten requests, wait five seconds, and reconnect.
python3 tools/emma_mock_client.py --host 192.168.88.20 --insecure --disconnect-after 10
```

Hardware-shape options:

| Option | Effect |
|---|---|
| `--smartguard-phase single\|three` | SmartGuard phase type; single-phase hides phase B/C entities as not applicable |
| `--grid-outage` | SmartGuard reports off-grid backup supply |
| `--scharger` | Adds a FusionCharge SCharger at unit 4 |
| `--write-failure exception` | Rejects every write with a Modbus exception |
| `--write-failure ignore` | Accepts writes but keeps the old value, so readback verification fails |
| `--unknown-enum` | Reports an undefined SmartGuard status value |
| `--no-tls` | Connects with plain TCP, for a server started with TLS disabled |

The same mock runs in-process in `tests/connector/test_simulated_emma.py`, which
drives the real reverse TLS server through discovery, polling, TOU writes, outages,
delays, disconnects, and read-only mode. Those tests are simulated hardware only;
they do not replace a test against a real EMMA.

## 4. Watch ESP32 behavior

Use ESPHome Device Builder logs or run `esphome logs` from the Home Assistant network.
Expected ESP32 messages include:

```text
EMMA TLS client connected; ESP32 is now Modbus master
Huawei startup model=EMMA-A02 ... serial=MOCK-EMMA-0001
Discovered 3 Huawei topology devices
EMMA core poll updated 10 entities
EMMA TOU readback contains 2 periods
```

The system LED shows timing-coded Modbus RX/TX activity. API activity appears only when
the separate workstation performs the following HTTP requests.

## 5. Pull values with curl from another machine

Run these commands on a workstation connected to the ESP32's Wi-Fi/Home Assistant
network. Use the ESP32 Wi-Fi address, not `192.168.88.20`.

Prompt for the API token without writing it into shell history:

```bash
read -rsp "ESP32 connector token: " EMMA_TEST_TOKEN
echo
```

Check authentication rejection first:

```bash
curl -i \
  -H "Authorization: Bearer deliberately-wrong-token" \
  http://192.168.1.194:8088/api/v1/health
```

The expected result is `HTTP/1.1 401 Unauthorized`. Then query with the correct token:

The ESP32 log records the rejected or accepted method/path and response status/size, but
never the configured or submitted token.

```bash
curl -sS -H "Authorization: Bearer $EMMA_TEST_TOKEN" \
  http://192.168.1.194:8088/api/v1/health | python3 -m json.tool

curl -sS -H "Authorization: Bearer $EMMA_TEST_TOKEN" \
  http://192.168.1.194:8088/api/v1/device | python3 -m json.tool

curl -sS -H "Authorization: Bearer $EMMA_TEST_TOKEN" \
  http://192.168.1.194:8088/api/v1/entities | python3 -m json.tool

curl -sS -H "Authorization: Bearer $EMMA_TEST_TOKEN" \
  http://192.168.1.194:8088/api/v1/states | python3 -m json.tool
```

The `/states` response should contain changing values such as
`pv_output_power`, `load_power`, and `feed_in_power`, plus `emma_tou_periods`.

Test a TOU write and immediate readback:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $EMMA_TEST_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"periods":[{"start_time":"00:00","end_time":"06:00","action":"charge","days":[true,true,true,true,true,true,true]},{"start_time":"17:00","end_time":"20:00","action":"discharge","days":[true,true,true,true,true,true,true]}]}' \
  http://192.168.1.194:8088/api/v1/tou-periods | python3 -m json.tool
```

The Armbian mock log should show an accepted 43-register function-16 write followed by
a function-3 readback. Remove the shell variable when finished:

```bash
unset EMMA_TEST_TOKEN
```

## 6. End the test

Stop the mock with `Ctrl+C`. Before reconnecting the real EMMA, confirm that the mock is
no longer reconnecting. Re-enable the HACS config entry only after the ESP32 test data is
no longer present, then perform the first real connection in read-only conditions.
