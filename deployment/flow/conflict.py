"""KiroCrew Flow — cron de resolução de conflito de merge.

Processa issues com ``flow:merge-conflict`` e despacha sessões one-shot
de resolução de conflito (rebase na mesma branch/PR).

Registro (uma vez):
    cron_add(name="flow-conflict",
             script="~/.kiro/crew/crons/deployment/flow/conflict.py:run",
             every=300)
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
    """Entrypoint do cron de conflito de merge.

    Processa issues com ``flow:merge-conflict`` e despacha sessões
    one-shot de resolução de conflito (rebase na mesma branch/PR).
    ``flow:review-refused`` é gate humano — apenas notifica TL, não redespacha.
    Intervalo recomendado: 300s.

    Configure o modelo via ``stage_models.conflito`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_CONFLITO)
