"""KiroCrew Flow — cron ORQUESTRADORA única (single-flow, ledger-driven).

Uma cron só, zero-token no scan. O estado da task vive no RunLedger (SQLite
local), NÃO nas labels do GitHub. A cada tick o motor lê a task ativa (1 query
local), lê o estado real da issue (1 chamada de rede) e a empurra estágio a
estágio — custo O(1) por tick, sem o scan que varria todos os estados.

Delega a ``deployment.run_single_flow``, que monta o RunLedger + provider +
adapters e chama ``flow.engine.ledger_tick.tick``. Só opera com
``single_flow: true`` na config.

Registro (uma vez):
    cron_add(name="flow-single",
             script="~/.kiro/crew/crons/deployment/flow/single_flow.py:run",
             every=60)
"""

from __future__ import annotations

import logging

try:
    from ..deployment import run_single_flow as _run_single_flow
except ImportError:
    # O cron runner carrega este arquivo via exec() sem pacote pai — o import
    # relativo falha. Carrega deployment.py por path absoluto como fallback.
    import importlib.util
    import os

    _DEPLOY_PY = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "deployment.py",
    )
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_deploy", _DEPLOY_PY)
    _mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    _run_single_flow = _mod.run_single_flow


logger = logging.getLogger(__name__)


def run(ctx: object) -> None:
    """Entrypoint ÚNICO do modo single-flow (ledger-driven).

    Delega ao motor em ``deployment.run_single_flow``. Resiliente: uma exceção
    do tick é logada e não derruba o cron.
    """
    try:
        _run_single_flow(ctx)
    except Exception as exc:  # resiliência do tick
        logger.error("deployment[single-flow]: tick falhou: %s", exc)
