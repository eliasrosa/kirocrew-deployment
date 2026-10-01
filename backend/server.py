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
from backend.engine.db import apply_migrations  # noqa: E402
from backend.version import get_version  # noqa: E402
from deployment.deployment import run_single_flow  # noqa: E402


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "app": "kirocrew-flow", "version": get_version()})


async def _single_flow_loop(interval: int) -> None:
    """Loop zero-token do single-flow.

    run_single_flow é síncrono (I/O com gh CLI e APIs) — rodado em executor
    para não bloquear o event loop do aiohttp.
    Token só gasto dentro do dispatcher quando há trabalho real.

    Args:
        interval: segundos entre cada ciclo
    """
    ctx = BackendCronCtx()
    while True:
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, run_single_flow, ctx)
        except Exception as exc:
            print(f"[crewflow single-flow loop] erro: {exc}", flush=True)
        await asyncio.sleep(interval)


async def start_background_loops(app: web.Application) -> None:
    apply_migrations()
    app["tasks"] = [
        asyncio.create_task(
            _single_flow_loop(
                int(os.environ.get("CREWFLOW_SINGLE_FLOW_INTERVAL", "60")),
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
