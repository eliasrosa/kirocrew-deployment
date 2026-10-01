"""
backend/engine/loader.py
Carrega e valida a definição YAML de um workflow.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import yaml  # pyyaml (opcional no pyproject.toml)
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# Tipos válidos de nó
NODE_TYPES = frozenset(
    {
        "trigger",
        "action/script",
        "action/open_session",
        "action/gate",       # aprovação humana (gap #3)
        "end",               # terminal de sucesso (gap #1)
        "fail",              # terminal de falha  (gap #1)
    }
)

# Campos obrigatórios por tipo
_REQUIRED: dict[str, list[str]] = {
    "trigger":            ["triggers", "next"],
    "action/script":      ["command", "next"],
    "action/open_session": ["instruction_path", "on_complete"],
    "action/gate":        ["prompt", "on_complete"],
    "end":                [],
    "fail":               [],
}


class WorkflowDefinition:
    def __init__(self, raw: dict[str, Any]) -> None:
        self._raw = raw
        self._validate()

    # ── Acesso ────────────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return self._raw["id"]

    @property
    def start(self) -> str:
        return self._raw["start"]

    @property
    def tick_interval_secs(self) -> int:
        return int(self._raw.get("tick_interval_secs", 60))

    @property
    def node_ttl_secs(self) -> int:
        """TTL padrão para nós open_session e gate (segundos). Default 1h."""
        return int(self._raw.get("node_ttl_secs", 3600))

    def get_node(self, node_id: str) -> dict[str, Any]:
        nodes = self._raw.get("nodes", {})
        if node_id not in nodes:
            raise KeyError(f"Nó '{node_id}' não encontrado no workflow '{self.id}'")
        return nodes[node_id]

    def node_ids(self) -> list[str]:
        return list(self._raw.get("nodes", {}).keys())

    # ── Validação ─────────────────────────────────────────────────────────

    def _validate(self) -> None:
        for field in ("id", "start", "nodes"):
            if field not in self._raw:
                raise ValueError(f"Campo obrigatório ausente no workflow: '{field}'")

        nodes = self._raw["nodes"]
        if self._raw["start"] not in nodes:
            raise ValueError(
                f"'start' aponta para '{self._raw['start']}' que não existe em nodes"
            )

        for node_id, node in nodes.items():
            node_type = node.get("type")
            if node_type not in NODE_TYPES:
                raise ValueError(
                    f"Nó '{node_id}': tipo '{node_type}' inválido. "
                    f"Válidos: {sorted(NODE_TYPES)}"
                )
            for req in _REQUIRED.get(node_type, []):
                if req not in node:
                    raise ValueError(
                        f"Nó '{node_id}' (tipo={node_type}): campo obrigatório '{req}' ausente"
                    )
            # Verifica que on_complete / next têm um `default`
            for cond_field in ("on_complete", "next"):
                conditions = node.get(cond_field)
                if isinstance(conditions, list):
                    has_default = any(
                        c.get("condition") == "default" for c in conditions
                    )
                    if not has_default:
                        raise ValueError(
                            f"Nó '{node_id}': '{cond_field}' não tem condição 'default'"
                        )


# ── Carregamento ───────────────────────────────────────────────────────────

# Cache em memória: { path_str -> WorkflowDefinition }
_cache: dict[str, WorkflowDefinition] = {}


def load(path: str | Path) -> WorkflowDefinition:
    """Carrega um arquivo YAML (ou JSON) e retorna um WorkflowDefinition.

    Resultado cacheado por path. Chamar reload() para invalidar.
    """
    path = Path(path)
    key = str(path.resolve())
    if key in _cache:
        return _cache[key]
    definition = _parse_file(path)
    _cache[key] = definition
    return definition


def reload(path: str | Path) -> WorkflowDefinition:
    """Força re-leitura do arquivo, invalida o cache para esse path."""
    key = str(Path(path).resolve())
    _cache.pop(key, None)
    return load(path)


def _parse_file(path: Path) -> WorkflowDefinition:
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        if yaml is None:
            raise ImportError(
                "pyyaml não instalado. "
                "Instale com: pip install 'kirocrew-flow[yaml]'"
            )
        raw = yaml.safe_load(text)
    elif path.suffix == ".json":
        raw = json.loads(text)
    else:
        raise ValueError(f"Formato não suportado: {path.suffix} (use .yaml ou .json)")
    return WorkflowDefinition(raw)
