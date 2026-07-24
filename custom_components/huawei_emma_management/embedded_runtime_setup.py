"""First-run environment and TLS certificate setup for both deployment modes."""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
@dataclass(frozen=True, slots=True)
class CertificateStatus:
    certificate: x509.Certificate
    ca_certificate: x509.Certificate | None
    created_ca: bool
    created_server: bool

    @property
    def fingerprint(self) -> str:
        source = self.ca_certificate or self.certificate
        raw = source.fingerprint(hashes.SHA256()).hex().upper()
        return ":".join(raw[index : index + 2] for index in range(0, len(raw), 2))


def load_or_create_environment(
    env_file: Path = Path(".env"), token_variable: str = "EMMA_API_TOKEN"
) -> tuple[str, bool]:
    """Load .env and persist a cryptographically random API token on first run."""
    from dotenv import load_dotenv, set_key

    load_dotenv(env_file, override=True)
    token = os.environ.get(token_variable, "").strip()
    if token:
        return token, False

    token = secrets.token_urlsafe(32)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    if not env_file.exists():
        env_file.touch(mode=0o600)
    set_key(str(env_file), token_variable, token, quote_mode="always")
    with _ignore_permission_error():
        env_file.chmod(0o600)
    os.environ[token_variable] = token
    return token, True


def ensure_certificates(
    cert_file: Path,
    key_file: Path,
    ca_cert_file: Path,
    ca_key_file: Path,
    server_name: str | None = None,
    server_key_password: str | None = None,
    allow_generate: bool = True,
) -> CertificateStatus:
    """Validate existing TLS material, or create a persistent local CA and leaf cert."""
    created_ca = False
    created_server = False
    now = dt.datetime.now(dt.timezone.utc)

    if not allow_generate:
        if not cert_file.is_file() or not key_file.is_file():
            raise RuntimeError(
                "Custom TLS mode requires existing certificate and private-key files"
            )
        existing_cert = _load_certificate(cert_file)
        existing_key = _load_private_key(key_file, server_key_password)
        _validate_key_pair(existing_cert, existing_key, "server")
        if not (
            _utc(existing_cert.not_valid_before_utc)
            <= now
            < _utc(existing_cert.not_valid_after_utc)
        ):
            raise RuntimeError("The custom TLS certificate is not currently valid")
        if server_name and not _certificate_covers_name(existing_cert, server_name):
            raise RuntimeError(
                "The custom TLS certificate subject alternative names do not contain "
                f"the configured server name or IP address: {server_name}"
            )
        return CertificateStatus(existing_cert, None, False, False)

    if cert_file.exists() and key_file.exists() and not (
        ca_cert_file.exists() or ca_key_file.exists()
    ):
        existing_cert = _load_certificate(cert_file)
        existing_key = _load_private_key(key_file, server_key_password)
        _validate_key_pair(existing_cert, existing_key, "server")
        not_before = _utc(existing_cert.not_valid_before_utc)
        not_after = _utc(existing_cert.not_valid_after_utc)
        if now < not_before or now >= not_after:
            raise RuntimeError(
                "The existing server certificate is not currently valid and has no local "
                "CA available for renewal. Replace it or remove both server files to generate "
                "a new local CA and certificate."
            )
        if server_name and not _certificate_covers_name(existing_cert, server_name):
            raise RuntimeError(
                "The custom TLS certificate subject alternative names do not contain "
                f"the configured server name or IP address: {server_name}"
            )
        return CertificateStatus(existing_cert, None, False, False)

    ca_cert: x509.Certificate
    ca_key: rsa.RSAPrivateKey
    if ca_cert_file.exists() and ca_key_file.exists():
        ca_cert = _load_certificate(ca_cert_file)
        ca_key = _load_private_key(ca_key_file)
        _validate_key_pair(ca_cert, ca_key, "CA")
        if not _utc(ca_cert.not_valid_before_utc) <= now < _utc(ca_cert.not_valid_after_utc):
            raise RuntimeError("The local TLS CA certificate is not currently valid")
    elif ca_cert_file.exists() or ca_key_file.exists():
        raise RuntimeError(
            "Incomplete TLS CA material: both the CA certificate and CA key are required"
        )
    else:
        ca_cert_file.parent.mkdir(parents=True, exist_ok=True)
        ca_key_file.parent.mkdir(parents=True, exist_ok=True)
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "Huawei EMMA Local Management CA")]
        )
        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=False,
                    key_cert_sign=True,
                    key_agreement=False,
                    content_commitment=False,
                    data_encipherment=False,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        _write_private_key(ca_key_file, ca_key)
        _write_certificate(ca_cert_file, ca_cert)
        created_ca = True

    renew_leaf = not (cert_file.exists() and key_file.exists())
    if cert_file.exists() != key_file.exists():
        raise RuntimeError(
            "Incomplete TLS server material: both the server certificate and key are required"
        )

    if not renew_leaf:
        server_cert = _load_certificate(cert_file)
        server_key = _load_private_key(key_file, server_key_password)
        _validate_key_pair(server_cert, server_key, "server")
        not_before = _utc(server_cert.not_valid_before_utc)
        not_after = _utc(server_cert.not_valid_after_utc)
        renew_leaf = now < not_before or now + dt.timedelta(days=30) >= not_after
        if server_name and not _certificate_covers_name(server_cert, server_name):
            renew_leaf = True

    if renew_leaf:
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.parent.mkdir(parents=True, exist_ok=True)
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        common_name = server_name or socket.gethostname() or "huawei-emma-server"
        sans = _subject_alternative_names(common_name)
        server_cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca_cert.subject)
            .public_key(server_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    key_encipherment=True,
                    key_cert_sign=False,
                    key_agreement=False,
                    content_commitment=False,
                    data_encipherment=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )
        _write_private_key(key_file, server_key, server_key_password)
        _write_certificate(cert_file, server_cert)
        created_server = True

    return CertificateStatus(server_cert, ca_cert, created_ca, created_server)


def certificate_days_remaining(cert_file: Path) -> int:
    certificate = _load_certificate(cert_file)
    remaining = _utc(certificate.not_valid_after_utc) - dt.datetime.now(dt.timezone.utc)
    return remaining.days


def _subject_alternative_names(common_name: str) -> list[x509.GeneralName]:
    try:
        names: list[x509.GeneralName] = [x509.IPAddress(ipaddress.ip_address(common_name))]
    except ValueError:
        names = [x509.DNSName(common_name)]
    hostname = socket.gethostname()
    if hostname and hostname != common_name:
        names.append(x509.DNSName(hostname))
    try:
        for item in socket.getaddrinfo(hostname, None):
            address = ipaddress.ip_address(item[4][0])
            if not address.is_loopback and x509.IPAddress(address) not in names:
                names.append(x509.IPAddress(address))
    except (OSError, ValueError):
        pass
    for destination in (("8.8.8.8", 53), ("1.1.1.1", 53)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(destination)
                address = ipaddress.ip_address(probe.getsockname()[0])
            candidate = x509.IPAddress(address)
            if not address.is_loopback and candidate not in names:
                names.append(candidate)
        except (OSError, ValueError):
            continue
    return names


def _certificate_covers_name(
    certificate: x509.Certificate, server_name: str
) -> bool:
    try:
        names = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        return False
    try:
        expected_ip = ipaddress.ip_address(server_name)
    except ValueError:
        expected_dns = server_name.rstrip(".").lower()
        return any(
            value.rstrip(".").lower() == expected_dns
            for value in names.get_values_for_type(x509.DNSName)
        )
    return expected_ip in names.get_values_for_type(x509.IPAddress)


def _load_certificate(path: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except Exception as error:
        raise RuntimeError(f"Cannot read TLS certificate {path}: {error}") from error


def _load_private_key(path: Path, password: str | None = None) -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(
            path.read_bytes(), password.encode("utf-8") if password else None
        )
    except Exception as error:
        raise RuntimeError(f"Cannot read TLS private key {path}: {error}") from error
    if not isinstance(key, rsa.RSAPrivateKey):
        raise RuntimeError(f"TLS private key {path} must be RSA")
    return key


def _validate_key_pair(
    certificate: x509.Certificate, key: rsa.RSAPrivateKey, label: str
) -> None:
    cert_public = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key_public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if cert_public != key_public:
        raise RuntimeError(f"TLS {label} certificate and private key do not match")


def _write_private_key(
    path: Path, key: rsa.RSAPrivateKey, password: str | None = None
) -> None:
    _atomic_write(
        path,
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(password.encode("utf-8"))
            if password
            else serialization.NoEncryption(),
        ),
    )
    with _ignore_permission_error():
        path.chmod(0o600)


def _write_certificate(path: Path, certificate: x509.Certificate) -> None:
    _atomic_write(path, certificate.public_bytes(serialization.Encoding.PEM))


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)


class _ignore_permission_error:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: BaseException | None, _tb: object) -> bool:
        return isinstance(exc, PermissionError)
