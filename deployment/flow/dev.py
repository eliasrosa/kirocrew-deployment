"""KiroCrew Flow — cron de implementação.

Processa issues em ``flow:develop-waiting`` e despacha sessões one-shot de dev.

Registro (uma vez):
    cron_add(name="flow-dev",
             script="~/.kiro/crew/crons/deployment/flow/dev.py:run",
             every=600)
"""

from __future__ import annotations

try:
    from .base import _STAGE_DEV, _run_stage
except ImportError:
    # O cron runner carrega este arquivo via exec() sem pacote pai — o import
    # relativo falha. Carrega base.py por path absoluto como fallback.
    import importlib.util
    import os

    _BASE_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "base.py")
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_base", _BASE_PY)
    _base = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_base)  # type: ignore[union-attr]
    _STAGE_DEV = _base._STAGE_DEV
    _run_stage = _base._run_stage


def run(ctx: object) -> None:
    """Entrypoint do cron de implementação.

    Processa issues em ``flow:develop-waiting`` e despacha sessões one-shot de dev.
    Ideal com um modelo forte (ex: sonnet-4.5) e intervalo de 600s.

    Configure o modelo via ``stage_models.dev`` na deployment.config.yaml.
    """
    _run_stage(ctx, _STAGE_DEV)
