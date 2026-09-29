"""Fonte única da versão do Crew App para os endpoints de health.

Contexto (issue #247, ver ``docs/RELEASE.md`` → *Fontes de verdade da versão*):
os endpoints de health **não** devem carregar uma string de versão hardcoded.
A versão é carimbada pelo ``python-semantic-release`` em duas fontes canônicas
(``pyproject.toml`` e ``app.json``); os endpoints derivam dela dinamicamente para
acompanhar automaticamente cada release, sem edição manual.

Ordem de resolução:

1. Metadados do pacote instalado (``importlib.metadata.version("kirocrew-flow")``)
   — a versão estampada em ``pyproject.toml`` e instalada via ``pip install -e``.
2. Fallback: campo ``version`` do ``app.json`` na raiz do repo (útil quando o
   pacote não está instalado, ex.: execução direta a partir do checkout).
3. Último fallback: ``"0.0.0"`` — string válida, para nunca quebrar o contrato do
   health (``test_backend_hooks_routes`` exige apenas a presença da chave).
"""
from __future__ import annotations

import json
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

_APP_JSON = Path(__file__).resolve().parent.parent / "app.json"
_FALLBACK_VERSION = "0.0.0"


def _version_from_app_json() -> str | None:
    try:
        manifest = json.loads(_APP_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = manifest.get("version")
    return value if isinstance(value, str) and value else None


@lru_cache(maxsize=1)
def get_version() -> str:
    """Retorna a versão do Crew App a partir da fonte única de verdade."""
    try:
        return _pkg_version("kirocrew-flow")
    except PackageNotFoundError:
        pass
    return _version_from_app_json() or _FALLBACK_VERSION
