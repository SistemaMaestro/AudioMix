"""
Self-signed TLS certificate for audiomix.local.

Generated once and stored in %LOCALAPPDATA%/AudioMix/.
The browser will show a cert warning on first use — user clicks
"Advanced → Proceed" once per device, after which HTTPS fetch from
the PWA works without further prompts.

The cert covers:
  DNS: audiomix.local, localhost
  IP:  127.0.0.1 + the detected LAN IP at generation time

Valid for 10 years so re-acceptance is very rare.
"""
from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

log = logging.getLogger("audiomix.cert")

_CERT_FILENAME = "audiomix-cert.pem"
_KEY_FILENAME  = "audiomix-key.pem"


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def ensure_cert(store_dir: Path) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating if missing."""
    store_dir.mkdir(parents=True, exist_ok=True)
    cert_path = store_dir / _CERT_FILENAME
    key_path  = store_dir / _KEY_FILENAME

    if cert_path.exists() and key_path.exists():
        log.info("TLS cert already exists at %s", cert_path)
        return cert_path, key_path

    log.info("Generating self-signed TLS cert for audiomix.local ...")
    lan_ip = _lan_ip()

    # RSA 2048 key
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "audiomix.local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AudioMix"),
    ])

    san = x509.SubjectAlternativeName([
        x509.DNSName("audiomix.local"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv4Address(lan_ip)),
    ])

    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))  # 10 years
        .add_extension(san, critical=False)
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .sign(key, hashes.SHA256())
    )

    cert_path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    log.info("TLS cert written to %s (covers LAN IP %s)", cert_path, lan_ip)
    return cert_path, key_path
