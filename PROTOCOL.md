# AudioMix Protocol — v1.0

Contract between AudioMix.py (Python gateway) and PWA clients.

- **Transport**: HTTP/1.1 (HTTPS recommended; self-signed cert in local mode).
- **Base URL**: `http://audiomix.local:47900` (primary) / last-known-IP (fallback) / user-entered.
- **Content type**: `application/json` in both directions, UTF-8.
- **Session header**: `X-AudioMix-Session: <session_token>` on every authenticated endpoint.

All responses follow the shape:

```json
{ "ok": true, ...payload... }
```

or on error:

```json
{ "ok": false, "reason": "ENUM_CODE", "message": "human message", "details": {...} }
```

Error codes: `INVALID_TOKEN`, `AUX_OCCUPIED`, `NOT_HOLDER`, `MIXER_OFFLINE`, `SESSION_EXPIRED`, `MISSING_SESSION`, `INVALID_INPUT`, `NOT_FOUND`, `FORBIDDEN`.

---

## Discovery

Before any other call the PWA must locate the server:

1. Try `http://audiomix.local:47900/api/ping` (mDNS).
2. If that fails, try `http://<last-known-ip>:47900/api/ping` (`localStorage.audiomix_server_url`).
3. If both fail, ask user to enter IP or QR-scan.

Save the URL that responded with `service == "audiomix"` into `localStorage`.

### `GET /api/ping`

Unauthenticated. Use to detect service + healthcheck.

**Response 200**
```json
{
  "ok": true,
  "service": "audiomix",
  "version": "1.0.0",
  "mixer_connected": true,
  "mixer_name": "StudioLive 32SC",
  "mixer_serial": "SD7E21060040",
  "mixer_host": "10.100.0.5"
}
```

`mixer_connected: false` still returns 200 — the AudioMix server itself is up, the mixer is just offline.

---

## Session lifecycle

One aux bus can only be controlled by one user at a time. Sessions are held in memory on the server; heartbeat every 5s, TTL 15s.

### `POST /api/session/claim`

Trade a Maestro token for an AudioMix session token bound to one aux.

**Request**
```http
POST /api/session/claim
Content-Type: application/json

{ "token": "<maestro token>", "aux_number": 3 }
```

**Response 200 (aux was free / was already held by same user)**
```json
{
  "ok": true,
  "session_token": "a1b2c3...",
  "user": { "id": "uuid", "nome": "João" },
  "aux": { "number": 3, "name": "Back 1" },
  "heartbeat_seconds": 5,
  "ttl_seconds": 15
}
```

**Response 401 — INVALID_TOKEN**: token rejected by Maestro.

**Response 409 — AUX_OCCUPIED**
```json
{
  "ok": false,
  "reason": "AUX_OCCUPIED",
  "holder": { "user_name": "Maria", "since": "2026-04-24T18:02:11+00:00" }
}
```

> Note: claiming aux B while you already hold aux A is legal and **auto-releases A**. One-aux-per-user invariant.

### `POST /api/session/heartbeat`

Every 5s the PWA pings to keep the lock alive.

**Request headers**: `X-AudioMix-Session: <token>`
**Response 200**: `{ "ok": true, "expires_in": 15 }`
**Response 410 — SESSION_EXPIRED**: PWA must re-`claim`.

### `POST /api/session/release`

Voluntary release (e.g. user navigates back).

**Response 200**: `{ "ok": true }`

### `GET /api/session/status`

Poll metadata about the current session.

**Response 200**
```json
{
  "ok": true,
  "aux_number": 3,
  "user": { "id": "uuid", "nome": "João" },
  "expires_in": 12
}
```

---

## Mixer reads

All authenticated (`X-AudioMix-Session` required).

### `GET /api/mixer/channels?type=line`

**Response 200**
```json
{
  "type": "line",
  "count": 32,
  "channels": [
    { "channel": 1, "name": "Kick",  "color": "ff0000ff", "mute": false },
    { "channel": 2, "name": "Caixa", "color": null,       "mute": false },
    ...
  ]
}
```

Supported `type`: `line` (default), `aux`, `fxbus`, `main`, `filtergroup`, `return`, `fxreturn`, `talkback`.

### `GET /api/mixer/auxes`

Lists all aux masters with current lock holder.

**Response 200**
```json
{
  "count": 16,
  "auxes": [
    { "number": 1, "name": "Pastor Cx",
      "locked_by": { "user_name": "João", "since": "2026-04-24T18:02:11+00:00" } },
    { "number": 2, "name": "Ministro",   "locked_by": null },
    ...
  ]
}
```

### `GET /api/mixer/aux/{n}/mix?source_type=line`

Only the holder of aux `n` can call this. Returns all 32 source channels' send levels for that aux.

**Response 200**
```json
{
  "aux_number": 3,
  "aux_name": "Back 1",
  "master_level": 0.72,
  "source_type": "line",
  "channels": [
    { "channel": 1, "name": "Kick",    "level": 0.0,   "mute": false },
    { "channel": 2, "name": "Caixa",   "level": 0.55,  "mute": false },
    ...
  ]
}
```

All `level` values are **0.0..1.0** (0 = -∞ dB, 0.72 ≈ 0 dB, 1.0 = +10 dB).

**Response 403 — NOT_HOLDER**: caller's session is bound to a different aux.

---

## Mixer writes

Holder-only. All levels sent as **0..100** (linear, 72 ≈ unity).

### `POST /api/mixer/aux/{n}/send`

```json
{ "source_channel": 16, "level": 72 }
```

Sets the aux send of line channel 16 into aux `n`.

### `POST /api/mixer/aux/{n}/master`

```json
{ "level": 72 }
```

Sets the master fader of aux `n`.

---

## Presets

User-scoped (`user_id` from Maestro). Stored in SQLite on the AudioMix server. Keyed by `(user_id, name)` unique. Levels in 0..1.

### `GET /api/presets`

```json
{
  "ok": true,
  "presets": [
    {
      "id": 7,
      "name": "Domingo manhã",
      "aux_number": 3,
      "master_level": 0.72,
      "created_at": "2026-04-20T14:10:00+00:00",
      "updated_at": "2026-04-24T18:02:11+00:00",
      "channels": [
        { "source_type": "line", "source_channel": 1, "level": 0.55, "hidden": false },
        { "source_type": "line", "source_channel": 2, "level": 0.62, "hidden": false },
        { "source_type": "line", "source_channel": 19, "level": 0.0, "hidden": true },
        ...
      ]
    }
  ]
}
```

### `POST /api/presets`

**Body**
```json
{
  "name": "Domingo manhã",
  "aux_number": 3,
  "master_level": 0.72,
  "channels": [
    { "source_type": "line", "source_channel": 1,  "level": 0.55, "hidden": false },
    { "source_type": "line", "source_channel": 19, "level": 0.0,  "hidden": true  }
  ]
}
```

**Response 200**: `{ "ok": true, "preset": {...} }`.

### `PUT /api/presets/{id}` — same body as create.

### `DELETE /api/presets/{id}`: `{ "ok": true }`.

### `POST /api/presets/{id}/apply?aux_number=<override>`

Applies a preset by sending all PVs to the mixer. `aux_number=0` (or omitted) uses the preset's saved aux. The caller must be the holder of the target aux.

**Response 200**
```json
{ "ok": true, "applied_to_aux": 3, "channels": 32 }
```

---

## PWA implementation checklist

- Open `/api/ping` on app startup; cache server URL in `localStorage.audiomix_server_url`.
- On `/mixer` route mount:
  1. `GET /api/mixer/auxes` to populate selector.
  2. User picks an aux → `POST /api/session/claim` with Maestro token.
  3. On 409 render "em uso por {holder.user_name}".
  4. On 200, start heartbeat `setInterval(..., 5000)`; fetch `/api/mixer/aux/{n}/mix` + `/api/presets`.
- Fader `oninput` → debounce 40–60ms → `POST /api/mixer/aux/{n}/send`. UI updates optimistically.
- On `beforeunload` / route leave → `POST /api/session/release` (fire-and-forget).
- On 410 (SESSION_EXPIRED) → automatic re-claim and resume.

### CORS

The PWA origin (`https://ipb.app.br` or localhost for dev) is whitelisted. Browser will block other origins.

### Mixed content

Default AudioMix listens on HTTP. If the PWA is served via HTTPS, you must run AudioMix with the provided self-signed cert and have the user accept it once.

---

## Admin interface (out-of-band)

`http://localhost:47900/admin` — bound to loopback only. HTML dashboard showing mesa status and active sessions, with a "kick" action per aux. Not reachable by PWA clients.
