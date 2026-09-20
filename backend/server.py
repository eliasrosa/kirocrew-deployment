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

from flow.scan.scanner import scan_candidates  # noqa: E402, F401


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "app": "kirocrew-flow", "version": "1.0.0"})


async def background_dev_loop(app: web.Application) -> None:
    """Loop zero-token para o estágio dev (crewflow:todo → DISPATCH_DEV)."""
    interval = int(os.environ.get("CREWFLOW_DEV_INTERVAL", "300"))
    while True:
        try:
            # TODO Fase 1b: chamar run_dev() do engine aqui
            pass
        except Exception as exc:
            print(f"[crewflow-dev loop] erro: {exc}", flush=True)
        await asyncio.sleep(interval)


async def start_background_loops(app: web.Application) -> None:
    app["tasks"] = [
        asyncio.create_task(background_dev_loop(app)),
        # TODO: reviewer, merge, conflito
    ]


async def stop_background_loops(app: web.Application) -> None:
    for task in app.get("tasks", []):
        task.cancel()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(start_background_loops)
    app.on_cleanup.append(stop_background_loops)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    web.run_app(build_app(), port=port)
