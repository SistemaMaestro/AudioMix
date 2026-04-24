"""
AudioMix — StudioLive III gateway for Maestro PWA.

Run:
    python AudioMix.py
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import uvicorn

from audiomix.app import create_app
from audiomix.cert import ensure_cert
from audiomix.config import load_settings


def _setup_logging(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    # Rotating file
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    # Calm down chatty libs
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main():
    settings = load_settings()
    _setup_logging(settings.storage.log_path_resolved)
    log = logging.getLogger("audiomix")
    log.info("launching AudioMix on %s:%d", settings.server.host, settings.server.port)
    log.info("logs: %s", settings.storage.log_path_resolved)
    log.info("db:   %s", settings.storage.db_path_resolved)

    app = create_app(settings)

    if settings.tls.is_external:
        cert_path = settings.tls.cert_path
        key_path = settings.tls.key_path
        log.info("HTTPS cert: %s (Let's Encrypt)", cert_path)
    else:
        cert_dir = settings.storage.db_path_resolved.parent
        cert_path, key_path = ensure_cert(cert_dir)
        log.info("HTTPS cert: %s (self-signed)", cert_path)
        log.info(
            "⚠️  First time on a new device: open https://audiomix.local:%d "
            "in the browser and accept the certificate warning.",
            settings.server.port,
        )

    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_level="info",
        access_log=False,
        ssl_certfile=str(cert_path),
        ssl_keyfile=str(key_path),
    )


if __name__ == "__main__":
    main()
