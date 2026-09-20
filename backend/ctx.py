"""Contexto sintético que simula o ctx do cron do Kiro Crew para uso nos loops asyncio."""
from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)


class BackendCronCtx:
    """Substitui o ctx injetado pelo Kiro Crew nos scripts de cron.

    O deployment.py usa ctx apenas para:
    - ctx.notify(text) — enviar mensagem ao usuário
    - ctx.message      — argumento do cron (normalmente vazio nos estágios)

    No loop asyncio do backend não existe o runtime do Kiro Crew, portanto:
    - notify() tenta entregar via gateway local (POST /api/chat/notify se disponível),
      fazendo fallback para log em caso de falha.
    - message é sempre uma string vazia (os estágios não usam ctx.message).
    """

    def __init__(self, message: str = "", gateway_port: int = 0) -> None:
        self.message: str = message
        self._gateway_port: int = gateway_port

    def notify(self, text: str) -> None:
        """Envia notificação via gateway local ou registra no log como fallback."""
        if self._gateway_port > 0:
            try:
                url = f"http://127.0.0.1:{self._gateway_port}/api/send_message"
                payload = json.dumps({"text": text}).encode()
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5):
                    pass
                return
            except Exception as exc:
                logger.debug("BackendCronCtx.notify: gateway indisponível (%s), usando log", exc)
        logger.info("crewflow notify: %s", text)
