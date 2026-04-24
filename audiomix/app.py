"""
FastAPI factory.

Builds the app with settings, middlewares, CORS and routes.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings, load_settings
from .lifecycle import lifespan
from .routes import public, admin

log = logging.getLogger("audiomix.app")


class AdminOnlyLocalhostMiddleware(BaseHTTPMiddleware):
    """Block /admin* from non-localhost clients."""
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/admin"):
            host = request.client.host if request.client else ""
            if host not in ("127.0.0.1", "::1", "localhost"):
                return JSONResponse(
                    {"ok": False, "reason": "FORBIDDEN",
                     "message": "admin UI is localhost-only"},
                    status_code=403,
                )
        return await call_next(request)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="AudioMix",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings

    # CORS: only the origins we trust
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.server.allowed_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-AudioMix-Session"],
    )

    if settings.server.admin_only_localhost:
        app.add_middleware(AdminOnlyLocalhostMiddleware)

    # Routes
    app.include_router(public.router, prefix="/api", tags=["public"])
    app.include_router(admin.router, prefix="/admin", tags=["admin"])

    # Admin static
    admin_static = Path(__file__).parent / "admin" / "static"
    if admin_static.exists():
        app.mount("/admin/static", StaticFiles(directory=admin_static), name="admin-static")

    @app.exception_handler(404)
    async def not_found(request, exc):
        return JSONResponse(
            {"ok": False, "reason": "NOT_FOUND", "path": request.url.path},
            status_code=404,
        )

    return app
