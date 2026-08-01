# Standalone Linux/Python connector

Use this runtime when Home Assistant cannot host the listener or when you want a separate
Linux host for development and diagnostics. It exposes the same authenticated connector
API consumed by the HACS integration and ESPHome-compatible tooling.

## Install and first start

Python 3.12 or newer is required:

```bash
git clone https://github.com/valexi7/Huawei-Modbus-TLS-Server /opt/huawei-emma
cd /opt/huawei-emma
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python modbus-server.py
```

The zero-argument start loads `.env`, creates a random `EMMA_API_TOKEN` when absent,
generates a persistent CA/server certificate when absent, validates existing TLS files,
and starts:

- reverse Modbus/TLS listener on port `16100`
- authenticated connector API on port `8088`

Read the generated API token locally:

```bash
grep '^EMMA_API_TOKEN=' /opt/huawei-emma/.env
```

Never commit `.env`, certificates, private keys, or tokens.

## Connect Home Assistant and EMMA

Configure the HACS integration in **External connector** mode with the Linux host,
port `8088`, and `EMMA_API_TOKEN`. Configure EMMA's third-party management-system
connection with the Linux host on port `16100`; import
`/opt/huawei-emma/certs/ca-cert.pem` into EMMA.

The HTTP bearer token protects Home Assistant-to-connector traffic. It is independent of
the TLS CA that EMMA trusts.

## systemd

The supplied unit expects this exact installation location:

```bash
sudo useradd --system --home-dir /opt/huawei-emma --shell /usr/sbin/nologin huawei-emma 2>/dev/null || true
sudo chown -R huawei-emma:huawei-emma /opt/huawei-emma
sudo cp /opt/huawei-emma/deploy/huawei-emma.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now huawei-emma.service
```

Check its health and logs:

```bash
sudo systemctl status huawei-emma.service
journalctl -u huawei-emma.service -f
```

If the unit reports `/opt/huawei-emma` does not exist, the project was installed in a
different directory. Move/clone it there or edit all unit paths consistently before
enabling the service.

## Upgrades

```bash
cd /opt/huawei-emma
sudo -u huawei-emma git pull --ff-only
sudo -u huawei-emma .venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart huawei-emma.service
```

The `.env` file and `certs/` directory are local runtime state and are not changed by a
normal Git pull. Review release notes before upgrades that change certificate, token, or
catalog behavior.

## Diagnostics

Use `--log-raw` for Modbus frame summaries:

```bash
sudo systemctl stop huawei-emma.service
cd /opt/huawei-emma
sudo -u huawei-emma .venv/bin/python modbus-server.py --log-raw
```

Do not use raw logs or screenshots to share bearer tokens. For direct API requests and
TOU examples, see [the connector API guide](huawei-emma-api.md). For ESP32 migration,
see [the LilyGO T-ETH-Elite guide](esphome-t-eth-elite.md).
