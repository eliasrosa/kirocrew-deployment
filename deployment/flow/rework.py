"""KiroCrew Flow — cron de re-trabalho pós-review.

Processa issues com ``flow:review-refused`` (gate humano: notifica TL/dev)
e issues com ``flow:merge-conflict`` para resolução via rebase.

O dispatch automático de rework só ocorre quando ``auto_dispatch: true`` na config.

Registro (uma vez):
    cron_add(name="flow-rework",
             script="~/.kiro/crew/crons/deployment/flow/rework.py:run",
             every=300)

Nota: internamente usa o mesmo estágio ``conflito`` de ``deployment.py``, que
agrupa tanto ``dispatch_rework`` quanto ``dispatch_conflict_resolver``.
"""

from __future__ import annotations

try:
    from .base import _STAGE_CONFLITO, _run_stage
except ImportError:
    import importlib.util
    import os

    _BASE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.py")
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_base", _BASE_PY)
    _base = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_base)  # type: ignore[union-attr]
    _STAGE_CONFLITO = _base._STAGE_CONFLITO
    _run_stage = _base._run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de re-trabalho pós-review e conflito de merge.

    Processa:
    - ``flow:review-refused``: gate humano — notifica TL, não redespacha automaticamente.
    - ``flow:merge-conflict``: despacha sessão one-shot de resolução de conflito.

    Configure o modelo via ``stage_models.conflito`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_CONFLITO)
