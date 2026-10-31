"""FastAPI application factory.

Health and version endpoints, the PROTOCOL v1 live routes (``/ws``, ``/api/state``,
``/api/command``, ``/frames/{id}.jpg``; see ``skypaw.live.ws``) bound to a
``MissionRuntime``, and the built frontend (frontend/dist) served at ``/`` when present.
Without an injected runtime the app builds an offline one (no simulation; fakes for the
Go2, detector and voice). Run with::

    uvicorn skypaw.app:create_app --factory --host 0.0.0.0 --port 7860
"""

from __future__ import annotations

import logging
import platform
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from skypaw import __version__
from skypaw.config import Settings, get_settings
from skypaw.live.ws import build_router
from skypaw.logging import configure_logging, get_logger, log_event
from skypaw.mission.runtime import MissionRuntime

log = get_logger(__name__)

PLACEHOLDER_HTML = (
    "<!doctype html><meta charset=utf-8><title>Sky &amp; Paw Ops</title>"
    "<p>Sky &amp; Paw backend is running; the frontend is not built yet "
    "(run <code>make build</code>).</p>"
)


def _ensure_data_dir(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # read-only or missing parent: later milestones need it, T0 does not
        log_event(
            log,
            "data_dir_unavailable",
            level=logging.WARNING,
            data_dir=str(path),
            error=str(exc),
        )


def create_app(settings: Settings | None = None, runtime: MissionRuntime | None = None) -> FastAPI:
    """Build the app. A passed ``runtime`` is started/stopped by its owner, not by the app."""
    settings = settings or get_settings()
    owns_runtime = runtime is None
    runtime = runtime or MissionRuntime(settings)
    configure_logging(settings.log_level)
    dist = settings.frontend_dist
    frontend_built = (dist / "index.html").is_file()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        _ensure_data_dir(settings.data_dir)
        log_event(
            log,
            "startup",
            version=__version__,
            env=settings.skypaw_env,
            state_source=settings.cw_state_source,
            frontend=str(dist) if frontend_built else None,
        )
        if owns_runtime:
            await runtime.start()
        try:
            yield
        finally:
            if owns_runtime:
                await runtime.stop()
            log_event(log, "shutdown")

    dev = settings.skypaw_env == "dev"
    app = FastAPI(
        title="Sky & Paw",
        version=__version__,
        lifespan=lifespan,
        # Interactive API docs only in dev; prod (Codespaces, Docker) exposes no /docs.
        docs_url="/docs" if dev else None,
        redoc_url="/redoc" if dev else None,
        openapi_url="/openapi.json" if dev else None,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins(),
        allow_origin_regex=settings.cors_origin_regex(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {
            "name": "skypaw",
            "version": __version__,
            "git_sha": settings.git_sha,
            "python": platform.python_version(),
            "state_source": settings.cw_state_source,
        }

    app.state.runtime = runtime
    app.include_router(build_router(runtime, settings))

    if frontend_built:
        # Mounted last so the API routes above take precedence.
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:
            return PLACEHOLDER_HTML

    return app


__all__ = ["PLACEHOLDER_HTML", "create_app"]
