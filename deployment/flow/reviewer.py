"""KiroCrew Flow — cron de code review.

Processa PRs em ``flow:review-waiting`` (sem ``flow:reviewed``) e despacha
sessões one-shot do kiro-reviewer.

Registro (uma vez):
    cron_add(name="flow-reviewer",
             script="~/.kiro/crew/crons/deployment/flow/reviewer.py:run",
             every=300)
"""

from __future__ import annotations

try:
    from .base import _STAGE_REVIEWER, _run_stage
except ImportError:
    import importlib.util
    import os

    _BASE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.py")
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_base", _BASE_PY)
    _base = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_base)  # type: ignore[union-attr]
    _STAGE_REVIEWER = _base._STAGE_REVIEWER
    _run_stage = _base._run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de code review.

    Processa PRs em ``flow:review-waiting`` (sem ``flow:reviewed``) e
    despacha sessões one-shot do kiro-reviewer.
    Ideal com um modelo mais rápido e intervalo de 300s.

    Configure o modelo via ``stage_models.reviewer`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_REVIEWER)
