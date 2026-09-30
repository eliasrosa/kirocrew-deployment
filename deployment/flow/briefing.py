"""KiroCrew Flow — cron de briefing (single-flow).

Processa issues em ``flow:briefing`` e despacha sessões one-shot de briefing.
Só produz ação quando ``single_flow`` está ativo na config; caso contrário o
executor mantém o comportamento paralelo (NOTIFY_HUMAN) e nada é despachado.

Registro (uma vez):
    cron_add(name="flow-briefing",
             script="~/.kiro/crew/crons/deployment/flow/briefing.py:run",
             every=600)
"""

from __future__ import annotations

try:
    from .base import _STAGE_BRIEFING, _run_stage
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
    _run_stage = _base._run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de briefing (single-flow).

    Processa issues em ``flow:briefing`` e despacha sessões one-shot que
    entendem a demanda e criam as 2 sub-tasks fixas. Sem efeito quando
    ``single_flow`` está desligado.
    """
    _run_stage(ctx, _STAGE_BRIEFING)
