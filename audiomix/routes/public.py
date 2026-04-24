"""
Public API consumed by the PWA (documented in PROTOCOL.md).

Prefix `/api`. Sessions identified by header `X-AudioMix-Session`.
"""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from .. import __version__
from ..auth import MaestroAuth, MaestroUser
from ..mixer_link import MixerLink
from ..presets import Preset, PresetChannel, PresetRepo
from ..sessions import AuxOccupied, AuxSession, SessionManager

log = logging.getLogger("audiomix.routes.public")
router = APIRouter()


# ---------------- DI helpers ----------------

def get_mixer(request: Request) -> MixerLink:
    return request.app.state.mixer

def get_auth(request: Request) -> MaestroAuth:
    return request.app.state.auth

def get_sessions(request: Request) -> SessionManager:
    return request.app.state.sessions

def get_presets(request: Request) -> PresetRepo:
    return request.app.state.presets


async def require_session(
    request: Request,
    x_audiomix_session: Annotated[Optional[str], Header()] = None,
) -> AuxSession:
    if not x_audiomix_session:
        raise HTTPException(status_code=401, detail={
            "ok": False, "reason": "MISSING_SESSION"
        })
    sessions: SessionManager = request.app.state.sessions
    sess = sessions.get_by_token(x_audiomix_session)
    if not sess:
        raise HTTPException(status_code=410, detail={
            "ok": False, "reason": "SESSION_EXPIRED"
        })
    return sess


# ---------------- Schemas ----------------

class ClaimBody(BaseModel):
    token: str
    aux_number: int = Field(ge=1, le=64)


class VolumeBody(BaseModel):
    source_channel: int = Field(ge=1, le=64)
    level: float = Field(ge=0, le=100)


class MasterBody(BaseModel):
    level: float = Field(ge=0, le=100)


class PresetChannelBody(BaseModel):
    source_type: str = "line"
    source_channel: int = Field(ge=1, le=64)
    level: float = Field(ge=0, le=1)
    hidden: bool = False


class PresetBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    aux_number: int = Field(ge=1, le=64)
    master_level: float = Field(ge=0, le=1, default=0.72)
    channels: list[PresetChannelBody] = Field(default_factory=list)


# ---------------- Discovery / health ----------------

@router.get("/ping")
async def ping(request: Request):
    mixer: MixerLink = request.app.state.mixer
    return {
        "ok": True,
        "service": "audiomix",
        "version": __version__,
        "mixer_connected": mixer.connected,
        "mixer_name": mixer.mixer_name,
        "mixer_serial": mixer.mixer_serial,
        "mixer_host": mixer.current_host,
    }


# ---------------- Session ----------------

@router.post("/session/claim")
async def claim(
    request: Request,
    body: ClaimBody,
    auth: Annotated[MaestroAuth, Depends(get_auth)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
):
    user = await auth.verify(body.token)
    if not user:
        raise HTTPException(status_code=401, detail={
            "ok": False, "reason": "INVALID_TOKEN",
            "message": "token rejeitado pelo Maestro",
        })
    if not user.can_mix:
        raise HTTPException(status_code=403, detail={
            "ok": False, "reason": "NOT_SCHEDULED",
            "message": "Você não está escalado para um culto nas próximas 3 horas.",
        })
    client_ip = request.client.host if request.client else "?"
    try:
        sess = await sessions.claim(user, body.aux_number, client_ip)
    except AuxOccupied as e:
        raise HTTPException(status_code=409, detail={
            "ok": False, "reason": "AUX_OCCUPIED",
            "holder": {
                "user_name": e.holder_name,
                "since": e.since.isoformat(),
            },
        })
    aux_name = request.app.state.mixer.get(f"aux/ch{body.aux_number}/username", f"Aux {body.aux_number}")
    return {
        "ok": True,
        "session_token": sess.session_token,
        "user": {"id": user.id, "nome": user.name},
        "aux": {"number": body.aux_number, "name": aux_name},
        "heartbeat_seconds": request.app.state.settings.session.heartbeat_seconds,
        "ttl_seconds": request.app.state.settings.session.ttl_seconds,
    }


@router.post("/session/heartbeat")
async def heartbeat(
    request: Request,
    sess: Annotated[AuxSession, Depends(require_session)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
):
    updated = await sessions.heartbeat(sess.session_token)
    if not updated:
        raise HTTPException(status_code=410, detail={
            "ok": False, "reason": "SESSION_EXPIRED",
        })
    return {"ok": True, "expires_in": sessions.expires_in(updated)}


@router.post("/session/release")
async def release(
    sess: Annotated[AuxSession, Depends(require_session)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
):
    await sessions.release(sess.session_token)
    return {"ok": True}


@router.get("/session/status")
async def session_status(
    sess: Annotated[AuxSession, Depends(require_session)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
):
    return {
        "ok": True,
        "aux_number": sess.aux_number,
        "user": {"id": sess.user_id, "nome": sess.user_name},
        "expires_in": sessions.expires_in(sess),
    }


# ---------------- Mixer reads ----------------

def _num_channels(mixer: MixerLink, prefix: str, max_n: int = 64) -> int:
    n = 0
    for i in range(1, max_n + 1):
        if mixer.get(f"{prefix}/ch{i}/username") is not None:
            n = i
    return n


@router.get("/mixer/channels")
async def mixer_channels(
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    sess: Annotated[AuxSession, Depends(require_session)],
    type: str = Query(default="line"),
):
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={
            "ok": False, "reason": "MIXER_OFFLINE",
        })
    n = _num_channels(mixer, type)
    return {
        "type": type,
        "count": n,
        "channels": [
            {
                "channel": i,
                "name": mixer.get(f"{type}/ch{i}/username", f"Ch. {i}"),
                "color": mixer.get(f"{type}/ch{i}/color"),
                "mute": bool(mixer.get(f"{type}/ch{i}/mute", 0)),
            }
            for i in range(1, n + 1)
        ],
    }


@router.get("/mixer/auxes")
async def mixer_auxes(
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    sessions: Annotated[SessionManager, Depends(get_sessions)],
    sess: Annotated[AuxSession, Depends(require_session)],
):
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={
            "ok": False, "reason": "MIXER_OFFLINE",
        })
    n = _num_channels(mixer, "aux")
    auxes = []
    for i in range(1, n + 1):
        holder = sessions.get_by_aux(i)
        auxes.append({
            "number": i,
            "name": mixer.get(f"aux/ch{i}/username", f"Aux {i}"),
            "locked_by": (
                {"user_name": holder.user_name, "since": holder.claimed_at.isoformat()}
                if holder else None
            ),
        })
    return {"count": n, "auxes": auxes}


@router.get("/mixer/aux/{aux_number}/mix")
async def aux_mix(
    aux_number: Annotated[int, Path(ge=1, le=64)],
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    sess: Annotated[AuxSession, Depends(require_session)],
    source_type: str = Query(default="line"),
):
    if sess.aux_number != aux_number:
        raise HTTPException(status_code=403, detail={
            "ok": False, "reason": "NOT_HOLDER",
            "message": f"you hold aux {sess.aux_number}, not {aux_number}",
        })
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={
            "ok": False, "reason": "MIXER_OFFLINE",
        })
    n = _num_channels(mixer, source_type)
    return {
        "aux_number": aux_number,
        "aux_name": mixer.get(f"aux/ch{aux_number}/username", f"Aux {aux_number}"),
        "master_level": mixer.get(f"aux/ch{aux_number}/volume", 0.0),
        "source_type": source_type,
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


# ---------------- Mixer writes ----------------

@router.post("/mixer/aux/{aux_number}/send")
async def mixer_aux_send(
    aux_number: Annotated[int, Path(ge=1, le=64)],
    body: VolumeBody,
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    sess: Annotated[AuxSession, Depends(require_session)],
):
    if sess.aux_number != aux_number:
        raise HTTPException(status_code=403, detail={
            "ok": False, "reason": "NOT_HOLDER",
        })
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={"ok": False, "reason": "MIXER_OFFLINE"})
    await mixer.set_aux_send("line", body.source_channel, aux_number, body.level)
    return {"ok": True}


@router.post("/mixer/aux/{aux_number}/master")
async def mixer_aux_master(
    aux_number: Annotated[int, Path(ge=1, le=64)],
    body: MasterBody,
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    sess: Annotated[AuxSession, Depends(require_session)],
):
    if sess.aux_number != aux_number:
        raise HTTPException(status_code=403, detail={"ok": False, "reason": "NOT_HOLDER"})
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={"ok": False, "reason": "MIXER_OFFLINE"})
    await mixer.set_volume("aux", aux_number, body.level)
    return {"ok": True}


# ---------------- Presets ----------------

def _preset_to_json(p: Preset) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "aux_number": p.aux_number,
        "master_level": p.master_level,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "channels": [
            {
                "source_type": c.source_type,
                "source_channel": c.source_channel,
                "level": c.level,
                "hidden": c.hidden,
            }
            for c in p.channels
        ],
    }


@router.get("/presets")
async def presets_list(
    repo: Annotated[PresetRepo, Depends(get_presets)],
    sess: Annotated[AuxSession, Depends(require_session)],
):
    items = await repo.list_for_user(sess.user_id)
    return {"ok": True, "presets": [_preset_to_json(p) for p in items]}


@router.post("/presets")
async def presets_create(
    body: PresetBody,
    repo: Annotated[PresetRepo, Depends(get_presets)],
    sess: Annotated[AuxSession, Depends(require_session)],
):
    channels = [PresetChannel(**c.model_dump()) for c in body.channels]
    created = await repo.create(
        user_id=sess.user_id,
        name=body.name,
        aux_number=body.aux_number,
        master_level=body.master_level,
        channels=channels,
    )
    return {"ok": True, "preset": _preset_to_json(created)}


@router.put("/presets/{preset_id}")
async def presets_update(
    preset_id: int,
    body: PresetBody,
    repo: Annotated[PresetRepo, Depends(get_presets)],
    sess: Annotated[AuxSession, Depends(require_session)],
):
    channels = [PresetChannel(**c.model_dump()) for c in body.channels]
    updated = await repo.update(
        user_id=sess.user_id,
        preset_id=preset_id,
        name=body.name,
        aux_number=body.aux_number,
        master_level=body.master_level,
        channels=channels,
    )
    if not updated:
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "NOT_FOUND"})
    return {"ok": True, "preset": _preset_to_json(updated)}


@router.delete("/presets/{preset_id}")
async def presets_delete(
    preset_id: int,
    repo: Annotated[PresetRepo, Depends(get_presets)],
    sess: Annotated[AuxSession, Depends(require_session)],
):
    ok = await repo.delete(sess.user_id, preset_id)
    if not ok:
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "NOT_FOUND"})
    return {"ok": True}


@router.post("/presets/{preset_id}/apply")
async def presets_apply(
    preset_id: int,
    repo: Annotated[PresetRepo, Depends(get_presets)],
    mixer: Annotated[MixerLink, Depends(get_mixer)],
    sess: Annotated[AuxSession, Depends(require_session)],
    aux_number: int = Query(default=0, ge=0, le=64, description="override preset aux; 0=use preset's aux"),
):
    preset = await repo.get(sess.user_id, preset_id)
    if not preset:
        raise HTTPException(status_code=404, detail={"ok": False, "reason": "NOT_FOUND"})
    if not mixer.connected:
        raise HTTPException(status_code=503, detail={"ok": False, "reason": "MIXER_OFFLINE"})

    target_aux = aux_number or preset.aux_number
    if sess.aux_number != target_aux:
        raise HTTPException(status_code=403, detail={
            "ok": False, "reason": "NOT_HOLDER",
            "message": f"you hold aux {sess.aux_number}, preset targets {target_aux}",
        })

    # Master
    await mixer.set_volume("aux", target_aux, preset.master_level * 100.0)
    # Channels
    for ch in preset.channels:
        await mixer.set_aux_send(ch.source_type, ch.source_channel, target_aux, ch.level * 100.0)

    return {"ok": True, "applied_to_aux": target_aux, "channels": len(preset.channels)}
