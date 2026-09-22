"""KiroCrew Flow — cron de resolução de conflito de merge.

Processa issues com ``flow:merge-conflict`` e despacha sessões one-shot
de resolução de conflito (rebase na mesma branch/PR).

Registro (uma vez):
    cron_add(name="flow-conflict",
             script="~/.kiro/crew/crons/deployment/flow/conflict.py:run",
             every=300)
"""

from __future__ import annotations

from .base import _STAGE_CONFLITO, _run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de conflito de merge.

    Processa issues com ``flow:merge-conflict`` e despacha sessões
    one-shot de resolução de conflito (rebase na mesma branch/PR).
    ``flow:review-refused`` é gate humano — apenas notifica TL, não redespacha.
    Intervalo recomendado: 300s.

    Configure o modelo via ``stage_models.conflito`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_CONFLITO)
