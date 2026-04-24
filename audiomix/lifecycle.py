"""
Application lifecycle: holds singletons, wires startup/shutdown.

All "services" are attached to `app.state` so routes can retrieve them via
Depends helpers in routes/.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .mixer_link import MixerLink
from .auth import MaestroAuth
from .sessions import SessionManager
from .presets import PresetRepo
from .mdns import MdnsAdvertiser
from . import __version__

log = logging.getLogger("audiomix.lifecycle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    log.info("AudioMix %s starting", __version__)

    mixer = MixerLink(settings.mixer)
    auth = MaestroAuth(settings.maestro)
    sessions = SessionManager(settings.session)
    presets = PresetRepo(settings.storage.db_path_resolved)
    mdns = MdnsAdvertiser(settings.mdns, settings.server.port, __version__)

    app.state.mixer = mixer
    app.state.auth = auth
    app.state.sessions = sessions
    app.state.presets = presets
    app.state.mdns = mdns

    await presets.init()
    await auth.start()
    await sessions.start()
    await mixer.start()
    await mdns.start()

    log.info("AudioMix ready on %s:%d", settings.server.host, settings.server.port)
    try:
        yield
    finally:
        log.info("AudioMix shutting down")
        await mdns.stop()
        await mixer.stop()
        await sessions.stop()
        await auth.stop()
