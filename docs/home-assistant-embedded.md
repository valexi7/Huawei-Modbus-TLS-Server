# Home Assistant integration and embedded TLS server

This is the recommended deployment: Home Assistant hosts the reverse Modbus/TLS
listener directly, while the Huawei EMMA Management integration owns the devices,
entities, polling subscriptions, controls, and schedules.

## Install

1. In **HACS > Integrations > Custom repositories**, add
   `https://github.com/valexi7/Huawei-Modbus-TLS-Server` as **Integration**.
2. Install **Huawei EMMA Management** and restart Home Assistant.
3. Open **Settings > Devices & services > Add integration** and select
   **Huawei EMMA Management**.
4. Choose **Run inside Home Assistant**.

Set a fixed Home Assistant IP address or stable DNS name and listener port `16100`.
EMMA must be able to open a TLS connection to that address. Do not forward this port to
the internet.

## Automatic certificates

With **Certificate source: Automatic**, the integration generates a private CA and a
leaf certificate under:

```text
/config/huawei_emma_management/<config-entry-id>/certs/
```

Import `ca-cert.pem` into EMMA's third-party management-system trust settings. Keep
`ca-key.pem` and `server-key.pem` private. HACS upgrades do not touch these files.

The leaf certificate is renewed while the local CA remains stable, so EMMA does not need
to trust a new CA during normal renewal.

## Custom certificates

Choose **Custom** to use an existing certificate and private key. Relative paths are
resolved from `/config`; Home Assistant OS commonly keeps public TLS material under
`/ssl`. The certificate SAN must include the exact address configured in EMMA. The
integration validates certificate dates and that the private key matches before binding.

## EMMA commissioning

In EMMA/FusionSolar third-party management settings, enable the management-system
connection and configure:

- server: Home Assistant fixed IP address or DNS name
- port: `16100`
- protocol: Standard Modbus
- TLS: enabled
- trusted CA: the generated or custom issuing CA

EMMA initiates the TCP/TLS connection. Once connected, Home Assistant acts as Modbus
master over that socket and discovers EMMA topology automatically.

## Entities and controls

The integration exposes its normal Home Assistant entities in embedded mode. Optional
registers start disabled; enable them from a device's **Entities** page. The connector
uses subscriptions and cached values, so Home Assistant's frequent coordinator refresh
does not create extra Modbus traffic.

For TOU schedules, safe controls, and direct API/action shapes, see
[the connector API guide](huawei-emma-api.md).

## Migrating to or from an external connector

The integration keeps entity ownership and unique IDs. Moving to ESPHome or Linux does
not require rebuilding dashboards or automations: set up the external connector, then
configure an external-mode entry with its host, port `8088`, and API token. Switching
modes is intentionally performed by removing and re-adding the config entry to prevent
an accidental options change from moving the control endpoint.

## Troubleshooting

- Confirm TCP `16100` is reachable from EMMA.
- Confirm the certificate SAN matches the address configured in EMMA.
- Import the CA certificate, not the server certificate or private key.
- Check Home Assistant logs for the listener bind error, TLS error, or parsed startup
  frame if EMMA does not connect.
