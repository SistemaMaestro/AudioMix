"""
Local admin dashboard — `http://localhost:47900/admin`.

Bound to localhost by AdminOnlyLocalhostMiddleware in app.py.
Provides read-only monitoring + force-release escape hatches.
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Annotated

import segno
from fastapi import APIRouter, Body, Depends, HTTPException, Path as PathParam, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .. import __version__
from ..admin_auth import ADMIN_TTL, COOKIE_NAME, AdminAuth
from ..mixer_link import MixerLink
from ..sessions import SessionManager

# Base URL of the Maestro PWA — the QR deep-links here so the phone's native
# camera opens the app, which then hands its token to AudioMix.
MAESTRO_PWA_URL = "https://app.maestro.mus.br"

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "admin" / "templates"))


def get_mixer(request: Request) -> MixerLink:
    return request.app.state.mixer

def get_sessions(request: Request) -> SessionManager:
    return request.app.state.sessions

def get_admin_auth(request: Request) -> AdminAuth:
    return request.app.state.admin_auth


def require_admin(request: Request):
    """Gate for write actions — the QR-paired admin cookie must be valid."""
    admin_auth: AdminAuth = request.app.state.admin_auth
    sess = admin_auth.validate(request.cookies.get(COOKIE_NAME))
    if not sess:
        raise HTTPException(status_code=401, detail={
            "ok": False, "reason": "MISSING_SESSION",
            "message": "faça login (QR) para editar",
        })
    return sess


class SendBody(BaseModel):
    source_channel: int = Field(ge=1, le=64)
    level: float = Field(ge=0, le=100)


class MasterBody(BaseModel):
    level: float = Field(ge=0, le=100)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return _templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"version": __version__},
    )


@router.get("/api/status")
async def admin_status(
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
):
    sess_list = sessions.all_sessions()
    now = time.monotonic()
    return {
        "version": __version__,
        "mixer": {
            "connected": mixer.connected,
            "host": mixer.current_host,
            "port": mixer.current_port,
            "name": mixer.mixer_name,
            "serial": mixer.mixer_serial,
            "connect_failures": mixer.connect_failures,
            "last_error": mixer.last_error,
            "state_keys": len(mixer.state),
        },
        "sessions": [
            {
                "aux_number": s.aux_number,
                "aux_name": mixer.get(f"aux/ch{s.aux_number}/username", f"Aux {s.aux_number}"),
                "user_id": s.user_id,
                "user_name": s.user_name,
                "client_ip": s.client_ip,
                "claimed_at": s.claimed_at.isoformat(),
                "age_seconds": int(now - (s.last_heartbeat - sessions.cfg.ttl_seconds + sessions.cfg.ttl_seconds)),
                "expires_in": sessions.expires_in(s),
            }
            for s in sorted(sess_list, key=lambda s: s.aux_number)
        ],
    }


def _num_channels(mixer: MixerLink, prefix: str, max_n: int = 64) -> int:
    n = 0
    for i in range(1, max_n + 1):
        if mixer.get(f"{prefix}/ch{i}/username") is not None:
            n = i
    return n


@router.get("/api/aux/{aux_number}")
async def admin_aux_mix(
    aux_number: Annotated[int, PathParam(ge=1, le=64)],
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
    source_type: str = "line",
):
    """Monitor mix of a single aux — every source channel's send level into it.

    Admin-only (localhost / LAN per config); unlike the public `/mixer/aux/*`
    route it has no holder check, so the operator can inspect any musician's mix.
    """
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={"ok": False, "reason": "MIXER_OFFLINE"})
    n = _num_channels(mixer, source_type)
    holder = sessions.get_by_aux(aux_number)
    return {
        "aux_number": aux_number,
        "aux_name": mixer.get(f"aux/ch{aux_number}/username", f"Aux {aux_number}"),
        "master_level": mixer.get(f"aux/ch{aux_number}/volume", 0.0),
        "source_type": source_type,
        "holder": (
            {"user_name": holder.user_name, "user_id": holder.user_id}
            if holder else None
        ),
        "channels": [
            {
                "channel": i,
                "name": mixer.get(f"{source_type}/ch{i}/username", f"Ch. {i}"),
                "level": mixer.get(f"{source_type}/ch{i}/aux{aux_number}", 0.0),
                "mute": bool(mixer.get(f"{source_type}/ch{i}/mute", 0)),
            }
            for i in range(1, n + 1)
        ],
    }


@router.post("/api/release/{aux_number}")
async def admin_release(
    aux_number: Annotated[int, PathParam(ge=1, le=64)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
    _admin: Annotated[object, Depends(require_admin)],
):
    ok = await sessions.force_release(aux_number)
    if not ok:
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "NOT_FOUND"})
    return {"ok": True}


# ---------------- Write actions (require paired admin) ----------------

@router.post("/api/aux/{aux_number}/send")
async def admin_set_send(
    aux_number: Annotated[int, PathParam(ge=1, le=64)],
    body: SendBody,
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    _admin: Annotated[object, Depends(require_admin)],
    source_type: str = "line",
):
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={"ok": False, "reason": "MIXER_OFFLINE"})
    await mixer.set_aux_send(source_type, body.source_channel, aux_number, body.level)
    return {"ok": True}


@router.post("/api/aux/{aux_number}/master")
async def admin_set_master(
    aux_number: Annotated[int, PathParam(ge=1, le=64)],
    body: MasterBody,
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    _admin: Annotated[object, Depends(require_admin)],
):
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={"ok": False, "reason": "MIXER_OFFLINE"})
    await mixer.set_volume("aux", aux_number, body.level)
    return {"ok": True}


# ---------------- Admin auth (QR pairing) ----------------

@router.get("/api/session")
async def admin_session_info(
    request: Request,
    admin_auth: Annotated[AdminAuth, Depends(get_admin_auth)],
):
    sess = admin_auth.validate(request.cookies.get(COOKIE_NAME))
    return {
        "authenticated": bool(sess),
        "user_name": sess.user_name if sess else None,
        "expires_in": int(sess.expires_at - time.time()) if sess else 0,
    }


@router.post("/auth/pair/new")
async def admin_pair_new(
    admin_auth: Annotated[AdminAuth, Depends(get_admin_auth)],
):
    pid, nonce, ttl = admin_auth.new_pairing()
    return {"pid": pid, "nonce": nonce, "expires_in": ttl}


@router.get("/auth/pair/{pid}/qr.svg")
async def admin_pair_qr(
    pid: str,
    admin_auth: Annotated[AdminAuth, Depends(get_admin_auth)],
):
    p = admin_auth.poll_pairing(pid)
    if not p or p.status != "pending":
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "NOT_FOUND"})
    url = f"{MAESTRO_PWA_URL}/audiomix-pair?pid={pid}&k={p.nonce}"
    buf = io.BytesIO()
    segno.make(url, error="m").save(
        buf, kind="svg", scale=6, border=2, dark="#350100", light="#ffffff"
    )
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


@router.get("/auth/pair/{pid}")
async def admin_pair_poll(
    pid: str,
    response: Response,
    admin_auth: Annotated[AdminAuth, Depends(get_admin_auth)],
):
    token = admin_auth.consume_pairing(pid)
    if token:
        response.set_cookie(
            COOKIE_NAME, token, max_age=ADMIN_TTL,
            httponly=True, secure=True, samesite="lax", path="/admin",
        )
        p = admin_auth.poll_pairing(pid)
        return {"status": "authorized", "user_name": p.user_name if p else ""}
    p = admin_auth.poll_pairing(pid)
    if not p:
        return {"status": "expired"}
    return {"status": p.status}


@router.post("/auth/pair/{pid}/claim")
async def admin_pair_claim(
    pid: str,
    admin_auth: Annotated[AdminAuth, Depends(get_admin_auth)],
    body: Annotated[dict, Body(...)],
):
    token = body.get("token") or body.get("maestro_token") or ""
    nonce = body.get("nonce") or ""
    result = await admin_auth.claim_pairing(pid, nonce, token)
    if not result["ok"]:
        code = {"NOT_FOUND": 404, "FORBIDDEN": 403, "INVALID_TOKEN": 401}.get(
            result["reason"], 400)
        raise HTTPException(status_code=code, detail=result)
    return result


@router.post("/auth/logout")
async def admin_logout(
    request: Request,
    response: Response,
    admin_auth: Annotated[AdminAuth, Depends(get_admin_auth)],
):
    admin_auth.logout(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/admin")
    return {"ok": True}
