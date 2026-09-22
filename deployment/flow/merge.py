"""KiroCrew Flow — cron de merge squash automático.

Processa PRs aprovados (``flow:review-approved`` ou ``flow:qa-approved``) e
executa o merge squash.

Registro (uma vez):
    cron_add(name="flow-merge",
             script="~/.kiro/crew/crons/deployment/flow/merge.py:run",
             every=120)
"""

from __future__ import annotations

from .base import _STAGE_MERGE, _run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de merge.

    Processa PRs aprovados (``flow:review-approved`` ou ``flow:qa-approved``) e
    executa o merge squash automático. Intervalo curto recomendado: 120s.

    Configure o modelo via ``stage_models.merge`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_MERGE)
