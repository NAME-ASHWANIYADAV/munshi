"""FastAPI application factory.

Startup does two things that matter: ensure the schema exists, and *probe* the configured
providers so the app knows — before the first request — whether it is serving Sarvam/Cognee/n8n or
their offline equivalents (SPEC.md §2.1).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from munshiji import __version__
from munshiji.api.routes import (
    actions,
    auth,
    chat,
    events,
    health,
    insights,
    khata,
    memory,
    merchant,
    voice,
)
from munshiji.config import get_settings
from munshiji.db.base import init_db
from munshiji.errors import MunshiJiError
from munshiji.logging import configure_logging, get_logger
from munshiji.providers.factory import build_providers, resolve, set_providers

logger = get_logger(__name__)

DESCRIPTION = """\
**MunshiJi** — the AI *munshi* for Indian merchants.

A voice-first business partner that knows the shop, advises the owner, and — with permission —
acts on their behalf. Built for the Paytm Build for India AI Hackathon, Delhi Edition 2026.

Every capability has a live vendor implementation (Sarvam / Cognee / n8n) and a fully functional
offline one; `GET /api/health` reports which is currently serving.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info(
        "starting MunshiJi %s env=%s provider_mode=%s",
        __version__,
        settings.env,
        settings.provider_mode,
    )

    init_db()

    bundle = await resolve(build_providers(settings), settings)
    set_providers(bundle)
    app.state.providers = bundle
    logger.info(
        "providers resolved: %s",
        ", ".join(f"{kind}={mode}" for kind, mode in bundle.modes.items()),
    )

    yield

    logger.info("shutting down")


def create_app() -> FastAPI:
    """Build the ASGI application."""
    app = FastAPI(
        title="MunshiJi API",
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    # The Vite dev server, plus whatever a deployment adds. MUNSHIJI_CORS_ORIGINS takes a
    # comma-separated list so a hosted companion screen can reach a hosted API without a rebuild;
    # a single "*" is accepted for a throwaway demo deployment, which is safe here only because
    # credentials are never sent and the API holds one seeded demo shop.
    extra_origins = [
        origin.strip()
        for origin in os.getenv("MUNSHIJI_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            *extra_origins,
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(MunshiJiError)
    async def _handle_app_error(_request: Request, exc: MunshiJiError) -> JSONResponse:
        """Expected application errors become clean JSON, not 500s."""
        if exc.status_code >= 500:
            logger.exception("unhandled application error: %s", exc.message)
        else:
            logger.info("%s: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    for module in (health, auth, merchant, insights, khata, chat, voice, actions, memory, events):
        app.include_router(module.router, prefix="/api")

    @app.get("/", include_in_schema=False)
    async def _root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    logger.debug("app created with %d routes", len(app.routes))
    return app


app = create_app()
