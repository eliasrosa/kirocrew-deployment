"""KiroCrew Flow — lógica comum compartilhada por todos os módulos de cron.

Este módulo localiza e importa o ``deployment.py`` pai (um nível acima desta pasta)
para expor ``_run_stage`` e as constantes de estágio aos módulos de cron em ``flow/``.

Funciona tanto no repo (``deployment/flow/``) quanto após instalação via
``scripts/install-cron.sh`` (``~/.kiro/crew/crons/deployment/flow/``):
o ``deployment.py`` sempre está um nível acima de ``flow/``.

Não expõe entrypoints de cron — use os módulos individuais (dev, reviewer, etc.).
"""

from __future__ import annotations

import importlib.util
import os
import sys

# Localiza o deployment.py um nível acima desta pasta (deployment/deployment.py
# no repo, ou ~/.kiro/crew/crons/deployment.py após instalação).
_FLOW_DIR = os.path.dirname(os.path.abspath(__file__))
_DEPLOYMENT_DIR = os.path.dirname(_FLOW_DIR)
_DEPLOYMENT_PY = os.path.join(_DEPLOYMENT_DIR, "deployment.py")

# Garante que o diretório pai de deployment/ esteja no sys.path para que
# o deployment.py possa importar flow/ (ele usa _REPO_ROOT / _FLOW_ROOT).
# O patch de sys.path do install-cron.sh cuida do _FLOW_ROOT — aqui apenas
# garantimos que o import do próprio deployment.py funcione.
if _DEPLOYMENT_DIR not in sys.path:
    sys.path.insert(0, _DEPLOYMENT_DIR)

# Carrega deployment.py como módulo se ainda não estiver em sys.modules.
# Usa o nome "_deployment_module" para evitar conflito com qualquer pacote
# "deployment" que possa estar instalado no ambiente.
_MOD_KEY = "_kirocrew_flow_deployment"
if _MOD_KEY not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MOD_KEY, _DEPLOYMENT_PY)
    if _spec is None or _spec.loader is None:
        raise ImportError(f"Não foi possível localizar deployment.py em: {_DEPLOYMENT_PY}")
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_MOD_KEY] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
else:
    _mod = sys.modules[_MOD_KEY]

# Re-exporta os símbolos necessários para os módulos de cron em flow/.
_run_stage = _mod._run_stage
_STAGE_DEV = _mod._STAGE_DEV
_STAGE_REVIEWER = _mod._STAGE_REVIEWER
_STAGE_MERGE = _mod._STAGE_MERGE
_STAGE_CONFLITO = _mod._STAGE_CONFLITO

__all__ = [
    "_STAGE_CONFLITO",
    "_STAGE_DEV",
    "_STAGE_MERGE",
    "_STAGE_REVIEWER",
    "_run_stage",
]
