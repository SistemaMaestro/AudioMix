"""
Settings loaded from `audiomix.toml` (optional) + environment overrides.

Env var pattern: AUDIOMIX_<SECTION>_<KEY>, e.g. AUDIOMIX_SERVER_PORT=47900.
"""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _expand(p: str) -> Path:
    """Expand %LOCALAPPDATA%, %USERPROFILE%, ~ and env vars into an absolute Path."""
    return Path(os.path.expandvars(os.path.expanduser(p))).resolve()


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 47900
    admin_only_localhost: bool = True
    allowed_cors_origins: list[str] = Field(
        default_factory=lambda: [
            "https://ipb.app.br",
            "https://www.ipb.app.br",
            "https://maestro.ipbrecreio.org.br",
            "http://localhost:3000",
            "http://localhost:5173",
        ]
    )


class MixerConfig(BaseModel):
    host: str = ""  # vazio = auto-discovery UDP
    port: int = 53000
    discovery_timeout_seconds: float = 8.0


class MaestroConfig(BaseModel):
    base_url: str = "https://api.ipb.app.br/api"
    token_cache_ttl_seconds: int = 300
    verify_token_path: str = "/Auth/verificar-token"
    request_timeout_seconds: float = 10.0


class SessionConfig(BaseModel):
    heartbeat_seconds: int = 5
    ttl_seconds: int = 15


class StorageConfig(BaseModel):
    db_path: str = "%LOCALAPPDATA%/AudioMix/audiomix.db"
    log_path: str = "%LOCALAPPDATA%/AudioMix/logs/audiomix.log"

    @property
    def db_path_resolved(self) -> Path:
        return _expand(self.db_path)

    @property
    def log_path_resolved(self) -> Path:
        return _expand(self.log_path)


class MdnsConfig(BaseModel):
    enabled: bool = True
    service_type: str = "_audiomix._tcp.local."
    instance_name: str = "AudioMix"
    hostname: str = "audiomix.local."


class TlsConfig(BaseModel):
    cert_file: str = ""  # path to PEM cert/chain; empty = use self-signed
    key_file: str = ""   # path to PEM private key; empty = use self-signed

    @property
    def cert_path(self) -> Optional[Path]:
        return _expand(self.cert_file) if self.cert_file else None

    @property
    def key_path(self) -> Optional[Path]:
        return _expand(self.key_file) if self.key_file else None

    @property
    def is_external(self) -> bool:
        p, k = self.cert_path, self.key_path
        return bool(p and k and p.exists() and k.exists())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUDIOMIX_",
        env_nested_delimiter="__",
        case_sensitive=False,
    )

    server: ServerConfig = ServerConfig()
    mixer: MixerConfig = MixerConfig()
    maestro: MaestroConfig = MaestroConfig()
    session: SessionConfig = SessionConfig()
    storage: StorageConfig = StorageConfig()
    mdns: MdnsConfig = MdnsConfig()
    tls: TlsConfig = TlsConfig()


def _config_candidates() -> list[Path]:
    # Search order: ./audiomix.toml, then %LOCALAPPDATA%/AudioMix/audiomix.toml
    here = Path(__file__).parent.parent / "audiomix.toml"
    user = _expand("%LOCALAPPDATA%/AudioMix/audiomix.toml")
    return [here, user]


def load_settings(explicit_path: Optional[Path] = None) -> Settings:
    overrides: dict = {}
    paths = [explicit_path] if explicit_path else _config_candidates()
    for p in paths:
        if p and p.exists():
            with open(p, "rb") as f:
                overrides = tomllib.load(f)
            break
    # Start from TOML overrides, env vars layered on top via Settings().
    base = Settings(**overrides) if overrides else Settings()
    return base


if __name__ == "__main__":
    s = load_settings()
    print("loaded settings:")
    print(s.model_dump_json(indent=2))
    print(f"\nDB path resolved: {s.storage.db_path_resolved}")
    print(f"Log path resolved: {s.storage.log_path_resolved}")
