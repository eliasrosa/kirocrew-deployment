"""KiroCrew Flow — cron de implementação.

Processa issues em ``flow:develop-waiting`` e despacha sessões one-shot de dev.

Registro (uma vez):
    cron_add(name="flow-dev",
             script="~/.kiro/crew/crons/deployment/flow/dev.py:run",
             every=600)
"""

from __future__ import annotations

from .base import _STAGE_DEV, _run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de implementação.

    Processa issues em ``flow:develop-waiting`` e despacha sessões one-shot de dev.
    Ideal com um modelo forte (ex: sonnet-4.5) e intervalo de 600s.

    Configure o modelo via ``stage_models.dev`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_DEV)
