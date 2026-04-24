"""
Local admin dashboard — `http://localhost:47900/admin`.

Bound to localhost by AdminOnlyLocalhostMiddleware in app.py.
Provides read-only monitoring + force-release escape hatches.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..mixer_link import MixerLink
from ..sessions import SessionManager

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "admin" / "templates"))


def get_mixer(request: Request) -> MixerLink:
    return request.app.state.mixer

def get_sessions(request: Request) -> SessionManager:
    return request.app.state.sessions


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


@router.post("/api/release/{aux_number}")
async def admin_release(
    aux_number: Annotated[int, PathParam(ge=1, le=64)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
):
    ok = await sessions.force_release(aux_number)
    if not ok:
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "NOT_FOUND"})
    return {"ok": True}
