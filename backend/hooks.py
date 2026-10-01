"""Hooks de ciclo de vida do app kirocrew-flow (in-gateway).

O gateway do Kiro Crew invoca os hooks de ciclo de vida declarados em
``app.json`` sob ``backend.hooks`` como ``result = func(ctx)`` (um único
argumento posicional = ``AppContext``, aguardado se for coroutine). Ver
``kiro_crew/apps/lifecycle.py:_invoke``. Portanto ``on_startup`` e
``on_shutdown`` DEVEM aceitar exatamente um argumento ``ctx`` — uma
assinatura de zero argumentos levanta ``TypeError``.

Estes hooks são os donos do ciclo de vida dos 4 loops asyncio de polling da
esteira (dev/reviewer/merge/conflito). As tasks criadas ficam em um registro
a nível de módulo (``_TASKS``) para que ``on_shutdown`` possa cancelá-las.

Nota: o gateway entrega um ``AppContext`` para este app, NÃO uma
``aiohttp.web.Application``. Por isso não dependemos de
``app.on_startup``/``app.on_cleanup`` aqui — quem faz esse caminho baseado em
aiohttp (para o servidor standalone / testes) é ``backend/routes.py``.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Registro a nível de módulo das tasks dos loops, para on_shutdown cancelar.
_TASKS: list[asyncio.Task] = []


async def on_startup(ctx: object) -> None:
    """Inicia o loop asyncio de polling do single-flow quando o app é habilitado.

    Invocado pelo gateway como ``func(ctx)`` (aguardado por ser coroutine).
    Reutiliza ``backend.server._single_flow_loop`` — importado de forma preguiçosa
    dentro da função (com o guard ``sys.path.insert(app_root)``) para funcionar
    sob o namespace de módulo sintético do gateway.

    Erros de import/startup são registrados (log), nunca propagados.
    """
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    try:
        from backend.server import _single_flow_loop

        _TASKS.clear()
        _TASKS.extend(
            [
                asyncio.create_task(
                    _single_flow_loop(
                        int(os.environ.get("CREWFLOW_SINGLE_FLOW_INTERVAL", "60")),
                    )
                ),
            ]
        )
        print("[kirocrew-flow] on_startup: single-flow loop iniciado", flush=True)
    except Exception as exc:
        print(f"[kirocrew-flow] on_startup error: {exc}", flush=True)


async def on_shutdown(ctx: object) -> None:
    """Cancela os loops asyncio e limpa o registro quando o app é desabilitado.

    Invocado pelo gateway como ``func(ctx)`` (aguardado por ser coroutine).
    """
    for task in _TASKS:
        task.cancel()
    _TASKS.clear()
    print("[kirocrew-flow] on_cleanup: loops cancelados", flush=True)
