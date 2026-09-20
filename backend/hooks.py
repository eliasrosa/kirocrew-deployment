"""Hooks de ciclo de vida do app kirocrew-flow (in-gateway).

Este arquivo existe para satisfazer o hook record do gateway que foi criado
quando o app.json tinha backend.hooks.on_startup / on_shutdown.
Os loops asyncio ficam dentro de register_routes em backend/routes.py
(via app.on_startup/on_cleanup), não aqui.
"""
from __future__ import annotations


def on_startup() -> None:
    """Invocado quando o app é habilitado. Noop — loops gerenciados por routes.py."""
    pass


def on_shutdown() -> None:
    """Invocado quando o app é desabilitado. Noop — loops cancelados por routes.py."""
    pass
