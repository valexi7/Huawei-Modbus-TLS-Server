# LilyGO T-ETH-Elite dual-network bring-up

The first firmware stage gives the ESP32-S3 two simultaneous interfaces:

- **Wi-Fi** connects to the Home Assistant network for native API, logs, and OTA.
- **ETH1/W5500** connects only to the EMMA management network using a fixed address.

This works with ESPHome **2026.4.0 or newer** through the repository's
`emma_w5500` external component. Released ESPHome 2026.7.3 still rejects a normal
configuration containing both `wifi:` and `ethernet:`. The upstream coexistence
change has merged for a future release, but using our dedicated component avoids
requiring an unreleased ESPHome build and gives the TLS listener an explicit ETH1.

The packaged W5500 pins come from LilyGO's official T-ETH-Elite definition:

| Signal | GPIO |
| --- | ---: |
| SCLK | 48 |
| MOSI | 21 |
| MISO | 47 |
| CS | 45 |
| Interrupt | 14 |
| Reset | not connected |

## Minimal device YAML

Copy [`esphome/huawei-emma-tls-server.yaml`](../esphome/huawei-emma-tls-server.yaml)
to the ESPHome configuration directory. It loads the maintained board package
directly from GitHub:

```yaml
packages:
  huawei_emma_board: github://valexi7/Huawei-Modbus-TLS-Server/esphome/packages/lilygo-t-eth-elite-dual-network.yaml@main

external_components:
  - source: github://valexi7/Huawei-Modbus-TLS-Server@main
    components: [emma_w5500]
    refresh: 1h
```

Add these local values to `secrets.yaml`:

```yaml
wifi_ssid: "HOME-ASSISTANT-WIFI"
wifi_password: "change-me"
huawei_emma_api_key: "BASE64-32-BYTE-ESPHOME-API-KEY"
huawei_emma_ota_password: "change-me"
emma_eth_ip: "192.168.88.20"
emma_eth_gateway: "192.168.88.1"
```

Choose an unused fixed `emma_eth_ip` in the same subnet as EMMA. The example
assumes EMMA is on `192.168.88.0/24`. The gateway is required by ESPHome's
manual-IP schema; it does not need to provide internet access, and no forwarding
or bridge between Wi-Fi and ETH1 is enabled.

Pin a release tag instead of `@main` after firmware becomes production-critical.
Using `@main` is useful during bring-up because ESPHome refreshes the package and
receives repository fixes automatically.

## Validate before flashing

1. Update the ESPHome Device Builder add-on to 2026.4 or newer.
2. Validate the YAML, then install once over USB.
3. Confirm the Wi-Fi address appears in Home Assistant and OTA works.
4. Connect ETH1 to the EMMA network and confirm link and the fixed address in logs.
5. From a host on the EMMA network, ping the fixed ETH1 address.

Do not connect ETH1 to the Home Assistant LAN. This design deliberately keeps
the management networks separate. ESPHome's native API may listen on all local
interfaces, but it is protected by API encryption and the isolated EMMA network
should permit only the future TLS listener port from EMMA.

## Next firmware stage

The package establishes only dual connectivity. It does **not yet** replace the
Python connector. The next external component will:

1. bind a TLS 1.2 listener to `${emma_eth_ip}:16100` only;
2. accept one EMMA client and parse the Huawei `0x41` startup frame;
3. perform paged device discovery and bounded Modbus reads/writes;
4. publish values and controls through ESPHome's encrypted native API;
5. store the dedicated EMMA certificate/key without logging private material.

Until that component is implemented and hardware-tested, keep the Armbian/Home
Assistant connector available for production control.
