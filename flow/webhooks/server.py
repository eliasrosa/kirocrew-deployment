"""Entrypoint standalone do servidor de webhooks.

Uso:
    python -m flow.webhooks.server

Variáveis de ambiente:
    WEBHOOK_SECRET  — segredo HMAC para validar assinaturas (opcional)
    WEBHOOK_PORT    — porta de escuta (padrão: 8765)
    WEBHOOK_HOST    — endereço de bind (padrão: 0.0.0.0)
"""

from __future__ import annotations

import logging
import os

from aiohttp import web

from flow.webhooks.handler import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> None:
    secret = os.environ.get("WEBHOOK_SECRET", "")
    port = int(os.environ.get("WEBHOOK_PORT", "8765"))
    host = os.environ.get("WEBHOOK_HOST", "0.0.0.0")

    app = create_app(webhook_secret=secret)
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
