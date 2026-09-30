"""KiroCrew Flow — cron ORQUESTRADORA única (single-flow).

Uma cron só, zero-token, que a cada tick percorre todos os estágios do
single-flow em sequência. Cada ``_run_stage`` faz o seu próprio scan
determinístico do estado (labels da issue + PR + RunLedger) e só age no
estágio que lhe cabe — o custo de LLM só ocorre quando um estágio
efetivamente dispara uma sessão de trabalho.

O RunLedger + ``max_concurrent=1`` garantem UMA task por vez percorrendo o
fluxo; varrer todos os estágios num único tick é seguro e barato.

Só produz ação quando ``single_flow`` está ligado na config; caso contrário
cada estágio mantém o comportamento paralelo (NOTIFY_HUMAN) e nada é
despachado.

Registro (uma vez):
    cron_add(name="flow-single",
             script="~/.kiro/crew/crons/deployment/flow/single_flow.py:run",
             every=60)
"""

from __future__ import annotations

import logging

try:
    from .base import (
        _STAGE_BRIEFING,
        _STAGE_CONFLITO,
        _STAGE_DEV,
        _STAGE_MERGE_QA,
        _STAGE_MERGE_REVIEW,
        _STAGE_PLANNING,
        _STAGE_PLANNING_REVIEW,
        _STAGE_REVIEWER,
        _run_stage,
    )
except ImportError:
    # O cron runner carrega este arquivo via exec() sem pacote pai — o import
    # relativo falha. Carrega base.py por path absoluto como fallback.
    import importlib.util
    import os

    _BASE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.py")
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_base", _BASE_PY)
    _base = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_base)  # type: ignore[union-attr]
    _STAGE_BRIEFING = _base._STAGE_BRIEFING
    _STAGE_CONFLITO = _base._STAGE_CONFLITO
    _STAGE_DEV = _base._STAGE_DEV
    _STAGE_MERGE_QA = _base._STAGE_MERGE_QA
    _STAGE_MERGE_REVIEW = _base._STAGE_MERGE_REVIEW
    _STAGE_PLANNING = _base._STAGE_PLANNING
    _STAGE_PLANNING_REVIEW = _base._STAGE_PLANNING_REVIEW
    _STAGE_REVIEWER = _base._STAGE_REVIEWER
    _run_stage = _base._run_stage


logger = logging.getLogger(__name__)


# Ordem do fluxo, do início ao fim. Espelha _SINGLE_FLOW_STAGES do monolítico.
_SINGLE_FLOW_STAGES = (
    _STAGE_BRIEFING,
    _STAGE_PLANNING,
    _STAGE_PLANNING_REVIEW,
    _STAGE_DEV,
    _STAGE_REVIEWER,
    _STAGE_MERGE_REVIEW,
    _STAGE_CONFLITO,
    _STAGE_MERGE_QA,
)


def run(ctx: object) -> None:
    """Entrypoint ÚNICO do modo single-flow.

    Percorre todos os estágios do single-flow em sequência num único tick.
    Resiliente por estágio: um estágio que falhe não interrompe os demais.
    """
    for stage in _SINGLE_FLOW_STAGES:
        try:
            _run_stage(ctx, stage)
        except Exception as exc:  # resiliência por estágio
            logger.error(
                "deployment[single-flow]: estágio %s falhou: %s", stage, exc
            )
