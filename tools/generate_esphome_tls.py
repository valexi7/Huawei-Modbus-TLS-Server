#!/usr/bin/env python3
"""Generate dedicated EMMA TLS material and an ESPHome secrets snippet."""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_setup import ensure_certificates


def _block(name: str, value: str) -> str:
    indented = "\n".join(f"  {line}" for line in value.rstrip().splitlines())
    return f"{name}: |-\n{indented}\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a local EMMA CA/server certificate and ESPHome secrets snippet"
    )
    parser.add_argument(
        "--server-name",
        default="192.168.88.20",
        help="ESP32 W5500 IP address or DNS name included in the server certificate",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("esphome/generated-certs"),
        help="Directory for generated CA/server PEM files",
    )
    parser.add_argument(
        "--secrets-snippet",
        type=Path,
        default=Path("esphome/emma-secrets.generated.yaml"),
        help="Sensitive YAML snippet to merge into the ESPHome secrets.yaml file",
    )
    args = parser.parse_args()

    output = args.output_dir.resolve()
    status = ensure_certificates(
        output / "server-cert.pem",
        output / "server-key.pem",
        output / "ca-cert.pem",
        output / "ca-key.pem",
        server_name=args.server_name,
    )
    certificate = (output / "server-cert.pem").read_text(encoding="utf-8")
    private_key = (output / "server-key.pem").read_text(encoding="utf-8")
    token = secrets.token_urlsafe(32)
    snippet = (
        f'emma_connector_api_token: "{token}"\n'
        + _block("emma_tls_certificate", certificate)
        + _block("emma_tls_private_key", private_key)
    )
    args.secrets_snippet.parent.mkdir(parents=True, exist_ok=True)
    args.secrets_snippet.write_text(snippet, encoding="utf-8")

    print(f"CA certificate: {output / 'ca-cert.pem'}")
    print(f"CA SHA-256 fingerprint: {status.fingerprint}")
    print(f"Sensitive ESPHome snippet: {args.secrets_snippet.resolve()}")
    print("Import ca-cert.pem into EMMA, then merge the snippet into secrets.yaml.")


if __name__ == "__main__":
    main()
