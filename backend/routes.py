"""Rotas do app kirocrew-flow registradas no gateway in-process."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web


async def _start_loops(app: web.Application) -> None:
    """Hook on_startup: inicia os 4 loops asyncio de polling da esteira."""
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    try:
        from backend.server import _run_stage_loop
        from deployment.deployment import (
            _STAGE_CONFLITO,
            _STAGE_DEV,
            _STAGE_MERGE,
            _STAGE_REVIEWER,
        )

        app["crewflow_tasks"] = [
            asyncio.create_task(
                _run_stage_loop(
                    _STAGE_DEV,
                    int(os.environ.get("CREWFLOW_DEV_INTERVAL", "300")),
                )
            ),
            asyncio.create_task(
                _run_stage_loop(
                    _STAGE_REVIEWER,
                    int(os.environ.get("CREWFLOW_REVIEWER_INTERVAL", "180")),
                )
            ),
            asyncio.create_task(
                _run_stage_loop(
                    _STAGE_MERGE,
                    int(os.environ.get("CREWFLOW_MERGE_INTERVAL", "120")),
                )
            ),
            asyncio.create_task(
                _run_stage_loop(
                    _STAGE_CONFLITO,
                    int(os.environ.get("CREWFLOW_CONFLITO_INTERVAL", "300")),
                )
            ),
        ]
        print("[kirocrew-flow] on_startup: 4 loops asyncio iniciados", flush=True)
    except Exception as exc:
        print(f"[kirocrew-flow] on_startup error: {exc}", flush=True)


async def _stop_loops(app: web.Application) -> None:
    """Hook on_cleanup: cancela os loops asyncio e limpa a lista."""
    for task in app.get("crewflow_tasks", []):
        task.cancel()
    app["crewflow_tasks"] = []
    print("[kirocrew-flow] on_cleanup: loops cancelados", flush=True)


def register_routes(app: web.Application) -> None:
    """Registra as rotas HTTP e hooks de ciclo de vida no app do gateway.

    Chamado pelo gateway com o ``web.Application`` de forma idêntica ao Issue Radar
    (``_mod.register_routes(app)``). Usa ``app.router`` para as rotas e
    ``app.on_startup`` / ``app.on_cleanup`` para os loops asyncio.

    Args:
        app: instância de ``aiohttp.web.Application`` do gateway.
    """
    app.router.add_get("/api/apps/kirocrew-flow/health", handle_health)
    app.router.add_get("/api/apps/kirocrew-flow/issues", handle_issues)
    app.router.add_post("/api/apps/kirocrew-flow/dispatch", handle_dispatch)

    app.on_startup.append(_start_loops)
    app.on_cleanup.append(_stop_loops)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "app": "kirocrew-flow", "version": "1.0.0"})


async def handle_issues(request: web.Request) -> web.Response:
    """Lista issues por estágio (crewflow:*). Placeholder para Fase 4."""
    return web.json_response({"issues": [], "note": "TODO Fase 4"})


async def handle_dispatch(request: web.Request) -> web.Response:
    """Force dispatch manual de uma issue. Placeholder para Fase 4."""
    return web.json_response({"ok": True, "note": "TODO Fase 4"})
