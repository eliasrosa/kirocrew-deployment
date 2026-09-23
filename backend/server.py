"""
Backend do app kirocrew-flow.
Expõe health check e loops zero-token de polling.
Importa de flow/ (sem renomear nesta fase).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web

# Garante que flow/ está no path (instalado via pip install -e .)
# O gateway injeta o app root no sys.path — mas ser explícito é seguro.
APP_ROOT = Path(__file__).parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.ctx import BackendCronCtx  # noqa: E402
from deployment.deployment import (  # noqa: E402
    _STAGE_CONFLITO,
    _STAGE_DEV,
    _STAGE_MERGE,
    _STAGE_REVIEWER,
    _run_stage,
)


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "app": "kirocrew-flow", "version": "1.0.0"})


async def _run_stage_loop(stage: str, interval: int) -> None:
    """Loop zero-token para um estágio da esteira.

    _run_stage é síncrono (I/O com gh CLI e APIs) — rodado em executor
    para não bloquear o event loop do aiohttp.
    Token só gasto dentro de _dispatch() quando há trabalho real.

    Args:
        stage:    um dos valores _STAGE_* (dev/reviewer/merge/conflito)
        interval: segundos entre cada ciclo
    """
    ctx = BackendCronCtx()
    while True:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _run_stage, ctx, stage)
        except Exception as exc:
            print(f"[crewflow-{stage} loop] erro: {exc}", flush=True)
        await asyncio.sleep(interval)


async def start_background_loops(app: web.Application) -> None:
    app["tasks"] = [
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


async def stop_background_loops(app: web.Application) -> None:
    for task in app.get("tasks", []):
        task.cancel()
    await asyncio.gather(*app.get("tasks", []), return_exceptions=True)


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(start_background_loops)
    app.on_cleanup.append(stop_background_loops)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    web.run_app(build_app(), port=port)
