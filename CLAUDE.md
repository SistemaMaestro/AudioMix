# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Windows-resident Python service (`AudioMix.py`) that bridges a PreSonus
StudioLive III mixer (UCNET, TCP 53000) to the Maestro PWA over HTTPS.
There are two layers worth keeping straight:

- `studiolive/` — low-level UCNET protocol, reverse-engineered from
  [featherbear/presonus-studiolive-api](https://github.com/featherbear/presonus-studiolive-api).
  Self-contained.
- `audiomix/` — the FastAPI service: discovery, sessions, presets, REST
  endpoints, admin dashboard. Talks to the mixer through `studiolive`.

The HTTP contract with the PWA lives in `PROTOCOL.md` and is **normative**
— route changes that affect the wire shape must update it too.

## Run & dev commands

- `python AudioMix.py` — entry point. Reads `audiomix.toml`, sets up
  rotating-file logging, ensures a TLS cert, runs uvicorn on
  `0.0.0.0:47900` over HTTPS.
- `run.bat` / `run.ps1 [-Install] [-Python py]` — Windows launchers that
  auto-`pip install -r requirements.txt` if the imports check fails.
- `pytest` — full suite. `pytest.ini` sets `asyncio_mode = auto` and
  `testpaths = tests`. Single test: `pytest tests/test_sessions.py::test_name`.
- `python -m audiomix.config` — dumps the resolved settings tree.
  Useful for debugging which TOML / env value won the merge.
- `install/install-service.ps1` — registers the Windows service.

## Configuration model

Three layers, each overrides the previous:

1. Defaults in `audiomix/config.py` (`Settings` pydantic model, with sub-models
   `ServerConfig`, `MixerConfig`, `MaestroConfig`, `SessionConfig`,
   `StorageConfig`, `MdnsConfig`, `TlsConfig`).
2. TOML file: `./audiomix.toml` first, then `%LOCALAPPDATA%/AudioMix/audiomix.toml`.
3. Env vars `AUDIOMIX_<SECTION>__<KEY>` — note the **double** underscore as
   nested delimiter (e.g. `AUDIOMIX_SERVER__PORT=47900`).

Setting `[tls] cert_file` + `key_file` switches off the self-signed flow and
uses the external cert (current Let's Encrypt setup keeps the PEMs at
`C:\audiomix.ipb.app.br-*.pem`).

## Architecture

### Wiring

`AudioMix.py` → `audiomix/app.py:create_app()` → `audiomix/lifecycle.py:lifespan()`.
The lifespan constructs five singletons (`MixerLink`, `MaestroAuth`,
`SessionManager`, `PresetRepo`, `MdnsAdvertiser`) and attaches them to
`app.state`. Routes pull them out via `Depends(get_*)` helpers — never import
the singletons directly.

### Mixer link (`audiomix/mixer_link.py`)

`MixerLink` owns one long-lived `StudioLiveClient`. It resolves the host (UDP
discovery on 47809 if `mixer.host` is empty), connects, waits for the initial
`ZB`/`CK` sync, then parks until the socket dies and reconnects with
exponential backoff (1s → 30s cap). After 3 failures with auto-discovery, it
forces re-discovery. The mixer's state is exposed as a flat `{path: value}`
dict (e.g. `line/ch1/volume → 0.72`, `line/ch1/aux3 → 0.45` for the send
of ch1 into aux 3, `aux/ch3/username → "Back 1"`); routes read these paths
directly via `mixer.get(path, default)`.

`MixerLink._require()` raises `RuntimeError("mixer offline")` when the link
is down; routes pre-check `mixer.connected` and return a 503 `MIXER_OFFLINE`
rather than let it surface.

### Low-level protocol (`studiolive/`)

- `protocol.py` — UCNET packet codec. Header `UC\x00\x01`, uint16 LE
  payload length, 2-char ASCII code (`KA` keepalive, `JM` subscribe,
  `PV` parameter value, `ZB`/`CK` initial state, `PS` strings, `FR/FD`
  file/health). `pv_float_packet(path, value)` is the one builder used
  for nearly every write.
- `state.py` — inflates the zlib + UBJSON `Synchronize` tree (`ZB`, or
  chunked `CK` re-assembled by `Chunker`) and flattens it. Also parses
  incremental `PV`/`PS`/`PC` updates.
- `client.py` — asyncio TCP client; auto-keepalive every 1s.
- `discovery.py` — UDP broadcast listener on 47809.
- `ubjson.py` — minimal UBJSON decoder.

**Path / scaling conventions**: `<type>/ch<N>/volume`, `<type>/ch<N>/mute`,
`<source>/ch<N>/aux<M>` for sends. On the wire levels are `0.0..1.0`
(0 = -84 dB, 0.72 ≈ unity, 1.0 = +10 dB). The public HTTP API exposes them
as **0..100**; conversion happens in `MixerLink.set_aux_send` /
`MixerLink.set_volume`. Don't double-scale.

### Sessions (`audiomix/sessions.py`)

In-memory locks, hard one-aux-per-user. `claim()` rotates a token on every
call (re-claiming the same aux as the same user is allowed), and
auto-releases any other aux the user was holding. A sweeper task expires
sessions older than `ttl_seconds` (default 15s); the PWA heartbeats every
`heartbeat_seconds` (default 5s). The token is the only credential for
write endpoints — sent as `X-AudioMix-Session` header (validated by
`require_session` in `routes/public.py`).

### Auth (`audiomix/auth.py`)

`MaestroAuth.verify(token)` calls Maestro
(`POST {base_url}{verify_token_path}` with `Authorization: Bearer`) and
caches successful results for `token_cache_ttl_seconds` (default 5 min).
The cache is single-flight (one `asyncio.Lock`) so a heartbeat storm
doesn't stampede Maestro. Returns a `MaestroUser` or `None`.

### Presets (`audiomix/presets.py`)

SQLite at `%LOCALAPPDATA%/AudioMix/audiomix.db`, schema in the `SCHEMA`
constant. Per-user (`user_id` from Maestro), unique `(user_id, name)`.
All access goes through `asyncio.to_thread` because `sqlite3` is blocking.
Levels are stored as `0..1` and only scaled to `0..100` when applied to
the mixer in `presets_apply`.

### Routes (`audiomix/routes/`)

- `public.py` mounted under `/api`. Discovery (`/ping`), sessions
  (`/session/*`), mixer reads/writes (`/mixer/*`), presets. Most endpoints
  depend on `require_session`; the deliberate exceptions are `/ping`,
  `/session/claim`, and `/mixer/auxes` (listing is public — see commit
  `eb40c09`).
- `admin.py` mounted under `/admin`. HTML dashboard + force-release.
  **Localhost-only** via `AdminOnlyLocalhostMiddleware` in `app.py`;
  non-loopback clients get 403.

### mDNS (`audiomix/mdns.py`)

Advertises `audiomix.local` so the PWA can find the box without a
hard-coded IP. Has a 3-attempt retry for `NonUniqueNameException`
(stale records from a killed prior instance).

### TLS (`audiomix/cert.py`)

If `[tls]` cert/key files exist, they're used as-is. Otherwise a
self-signed cert covering `audiomix.local`, `localhost`, `127.0.0.1`,
and the detected LAN IP is generated once into `%LOCALAPPDATA%/AudioMix/`
(10-year validity).

## Wire contract (`PROTOCOL.md`)

Every response is `{"ok": true, ...}` or
`{"ok": false, "reason": "ENUM", ...}`. The error enum is fixed and the
PWA branches on it: `INVALID_TOKEN`, `AUX_OCCUPIED`, `NOT_HOLDER`,
`MIXER_OFFLINE`, `SESSION_EXPIRED`, `MISSING_SESSION`, `INVALID_INPUT`,
`NOT_FOUND`, `FORBIDDEN`. Don't introduce new codes without updating
`PROTOCOL.md`.

CORS allowlist lives in `ServerConfig.allowed_cors_origins`; adding a
new PWA origin requires editing it (or overriding via TOML).

## Conventions

- Channel numbers are 1-indexed everywhere (matches the mixer UI).
- Aux numbers are validated `1..64` at the route layer even though the
  StudioLive 32SC tops out at 16 — the bound is generic for other
  III-series consoles.
- Logging is namespaced `audiomix.<module>`; the rotating file lands at
  `%LOCALAPPDATA%/AudioMix/logs/audiomix.log` (10 MB × 5).
- User-facing text (commit messages, log strings shown to operators,
  README) is Portuguese (pt-BR); code, identifiers, and module
  docstrings are English. Match the existing style.
