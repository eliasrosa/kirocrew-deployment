#!/usr/bin/env python3
"""Script de preparação para o semantic-release.

Atualiza ``app.json`` e ``pyproject.toml`` com a nova versão antes do commit
de release.

Uso (invocado pelo semantic-release via ``prepare`` no .releaserc.json):
    python3 scripts/prepare-release.py <VERSÃO>

Ou diretamente:
    NEXT_RELEASE_VERSION=1.2.3 python3 scripts/prepare-release.py
"""

from __future__ import annotations

import json
import os
import re
import sys


def _update_app_json(version: str, path: str = "app.json") -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    data["version"] = version

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
        f.write("\n")
    print(f"  app.json → {version}")


def _update_pyproject(version: str, path: str = "pyproject.toml") -> None:
    with open(path, encoding="utf-8") as f:
        content = f.read()

    updated = re.sub(
        r'^(version\s*=\s*")[^"]+(")',
        rf'\g<1>{version}\g<2>',
        content,
        flags=re.MULTILINE,
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    print(f"  pyproject.toml → {version}")


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NEXT_RELEASE_VERSION", "")
    if not version:
        print("Uso: prepare-release.py <VERSÃO>  ou  NEXT_RELEASE_VERSION=x.y.z prepare-release.py")
        sys.exit(1)

    # Remove prefixo 'v' se presente (semantic-release às vezes passa 'v1.2.3')
    version = version.lstrip("v")

    print(f"Preparando release {version}…")
    _update_app_json(version)
    _update_pyproject(version)
    print("Pronto.")


if __name__ == "__main__":
    main()
