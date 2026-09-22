"""KiroCrew Flow — cron de merge squash automático.

Processa PRs aprovados (``flow:review-approved`` ou ``flow:qa-approved``) e
executa o merge squash.

Registro (uma vez):
    cron_add(name="flow-merge",
             script="~/.kiro/crew/crons/deployment/flow/merge.py:run",
             every=120)
"""

from __future__ import annotations

try:
    from .base import _STAGE_MERGE, _run_stage
except ImportError:
    import importlib.util
    import os

    _BASE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.py")
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_base", _BASE_PY)
    _base = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_base)  # type: ignore[union-attr]
    _STAGE_MERGE = _base._STAGE_MERGE
    _run_stage = _base._run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de merge.

    Processa PRs aprovados (``flow:review-approved`` ou ``flow:qa-approved``) e
    executa o merge squash automático. Intervalo curto recomendado: 120s.

    Configure o modelo via ``stage_models.merge`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_MERGE)
