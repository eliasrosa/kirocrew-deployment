"""KiroCrew Flow — cron de especificação / planning-specs (single-flow).

Processa issues em ``flow:planning-specs`` e despacha sessões one-shot que
montam a spec padrão Kiro (requirements + design + tasks) na Sub-task 1.
Só produz ação quando ``single_flow`` está ativo na config; caso contrário o
executor mantém o comportamento paralelo (SKIP) e nada é despachado.

Registro (uma vez):
    cron_add(name="flow-planning",
             script="~/.kiro/crew/crons/deployment/flow/planning_specs.py:run",
             every=600)
"""

from __future__ import annotations

try:
    from .base import _STAGE_PLANNING, _run_stage
except ImportError:
    # O cron runner carrega este arquivo via exec() sem pacote pai — o import
    # relativo falha. Carrega base.py por path absoluto como fallback.
    import importlib.util
    import os

    _BASE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.py")
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_base", _BASE_PY)
    _base = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_base)  # type: ignore[union-attr]
    _STAGE_PLANNING = _base._STAGE_PLANNING
    _run_stage = _base._run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de especificação (single-flow).

    Processa issues em ``flow:planning-specs`` e despacha sessões one-shot que
    montam a spec e transicionam para ``flow:planning-review``. Sem efeito
    quando ``single_flow`` está desligado.
    """
    _run_stage(ctx, _STAGE_PLANNING)
