#!/usr/bin/env python3
"""Sincroniza a versão do ``app.json`` com a versão canônica do ``pyproject.toml``.

Contexto (issue #247, ver ``docs/RELEASE.md`` → *Fontes de verdade da versão*):
o ``python-semantic-release`` carimba ``[project].version`` no ``pyproject.toml``
via ``version_toml``, mas **não** sabe atualizar uma chave arbitrária de um JSON.
O ``app.json`` (manifesto do Crew App que o gateway lê) também precisa carregar a
mesma versão. Este helper é invocado pelo ``build_command`` do
``python-semantic-release`` (a cada release, depois que o ``pyproject.toml`` já foi
estampado) e propaga a versão para o ``app.json``, mantendo uma única fonte de
verdade e o ``app.json`` sempre em sincronia.

Uso::

    python scripts/sync_app_version.py            # lê a versão do pyproject.toml
    python scripts/sync_app_version.py 1.2.3      # usa a versão passada (ex.: $NEW_VERSION)

O ``python-semantic-release`` expõe a nova versão em ``$NEW_VERSION`` durante o
``build_command``; quando presente, ela é usada de forma preferencial. Caso
contrário, a versão é lida de ``pyproject.toml`` (que o semantic-release já
estampou antes de rodar o build).

O script preserva a indentação de 4 espaços e o newline final do ``app.json`` para
manter o diff mínimo e o arquivo estável. Não importa nenhuma lib de I/O de
``flow/`` — vive em ``scripts/`` justamente para respeitar o isolamento de
``flow/domain/`` (ver ``.kiro/steering/arquitetura.md`` e ``test_domain_boundary``).
"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_APP_JSON = _REPO_ROOT / "app.json"


def _version_from_pyproject() -> str:
    with _PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit("pyproject.toml: [project].version ausente ou inválida")
    return version


def _resolve_version(argv: list[str]) -> str:
    """Resolve a versão-alvo: argumento explícito > $NEW_VERSION > pyproject.toml."""
    if len(argv) > 1 and argv[1].strip():
        return argv[1].strip()
    env_version = os.environ.get("NEW_VERSION", "").strip()
    if env_version:
        return env_version
    return _version_from_pyproject()


def sync_app_version(version: str) -> bool:
    """Escreve ``version`` na chave ``version`` do ``app.json``.

    Retorna ``True`` se o arquivo mudou, ``False`` se já estava sincronizado.
    """
    raw = _APP_JSON.read_text(encoding="utf-8")
    trailing_newline = raw.endswith("\n")
    manifest = json.loads(raw)

    if manifest.get("version") == version:
        return False

    manifest["version"] = version
    serialized = json.dumps(manifest, indent=4, ensure_ascii=False)
    if trailing_newline:
        serialized += "\n"
    _APP_JSON.write_text(serialized, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    version = _resolve_version(argv)
    changed = sync_app_version(version)
    action = "atualizado" if changed else "já em sincronia"
    print(f"[sync_app_version] app.json {action} → version {version}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
