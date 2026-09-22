"""KiroCrew Flow — cron de code review.

Processa PRs em ``flow:review-waiting`` (sem ``flow:reviewed``) e despacha
sessões one-shot do kiro-reviewer.

Registro (uma vez):
    cron_add(name="flow-reviewer",
             script="~/.kiro/crew/crons/deployment/flow/reviewer.py:run",
             every=300)
"""

from __future__ import annotations

from .base import _STAGE_REVIEWER, _run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de code review.

    Processa PRs em ``flow:review-waiting`` (sem ``flow:reviewed``) e
    despacha sessões one-shot do kiro-reviewer.
    Ideal com um modelo mais rápido e intervalo de 300s.

    Configure o modelo via ``stage_models.reviewer`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_REVIEWER)
