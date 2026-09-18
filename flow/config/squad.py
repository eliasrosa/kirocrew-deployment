"""Modelo e parser de configuração de squad.

Um arquivo squads/<id>.yaml define:
  - qual provider usar (github ou jira)
  - quais projetos/repos varrer
  - qual template de workflow usar
  - as regras de routing de issue → workflow
  - parâmetros do template

O schema é documentado em squads/example.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Erros de validação
# ---------------------------------------------------------------------------

class SquadConfigError(ValueError):
    """Erro de validação na configuração de squad."""


# ---------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RoutingRule:
    """Uma regra de routing: se a issue tem estas labels → usar este workflow."""
    labels: frozenset[str]   # todas devem estar presentes (AND)
    workflow: str            # nome do workflow a usar


@dataclass(frozen=True, slots=True)
class WorkflowParams:
    """Parâmetros configuráveis do template de workflow."""
    merge_mode: str = "manual"        # "manual" | "auto" (futuro)
    review_position: str = "before_qa"  # Versão C: before_qa | after_qa | parallel_qa
    deploy_hml_mode: str = "manual"   # "manual" | "auto" (futuro)
    allow_hml_bypass: bool = True     # hotfix pode ir direto pra PRD


@dataclass(slots=True)
class SquadConfig:
    """Configuração de uma squad.

    Mutável para facilitar merge de defaults após o parse.
    """

    id: str
    name: str
    issue_provider: str          # "github" | "jira"
    projects: list[str]          # projetos/repos a varrer
    repos: frozenset[str]        # repos conhecidos (para validação de título)
    workflow_template: str       # nome do template fixo (Fase 1)
    workflow_params: WorkflowParams = field(default_factory=WorkflowParams)
    routing: list[RoutingRule] = field(default_factory=list)
    default_workflow: str = "feature-flow"

    def resolve_workflow(self, labels: frozenset[str]) -> str:
        """Retorna o nome do workflow para um conjunto de labels.

        Avalia as regras em ordem; retorna o default se nenhuma casar.
        """
        for rule in self.routing:
            if rule.labels <= labels:  # todas as labels da regra estão presentes
                return rule.workflow
        return self.default_workflow


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def load_squad(path: str | Path) -> SquadConfig:
    """Carrega e valida um arquivo de config de squad.

    Aceita YAML (via PyYAML se disponível, senão fallback para parser mínimo).
    Lança SquadConfigError com mensagem clara se algo estiver errado.
    """
    raw = _read_yaml(Path(path))
    return _parse_squad(raw, source=str(path))


def load_squads_dir(directory: str | Path) -> list[SquadConfig]:
    """Carrega todos os arquivos .yaml de um diretório de squads."""
    squads: list[SquadConfig] = []
    for yaml_file in sorted(Path(directory).glob("*.yaml")):
        if yaml_file.name.startswith("_") or yaml_file.name == "example.yaml":
            continue
        squads.append(load_squad(yaml_file))
    return squads


def _parse_squad(raw: dict[str, Any], source: str = "<dict>") -> SquadConfig:
    """Parseia e valida um dict bruto para SquadConfig."""
    _require(raw, "id", source)
    _require(raw, "issue_provider", source)

    squad_id = _str(raw, "id", source)
    name = raw.get("name") or squad_id
    provider = _str(raw, "issue_provider", source)

    if provider not in ("github", "jira"):
        raise SquadConfigError(
            f"{source}: issue_provider deve ser 'github' ou 'jira', não {provider!r}"
        )

    # projetos: campo "project" (Jira) ou "repos" (GitHub) — ou ambos
    projects: list[str] = []
    if raw.get("project"):
        projects.append(str(raw["project"]))
    projects.extend(raw.get("repos") or [])
    if not projects:
        raise SquadConfigError(
            f"{source}: squad deve ter 'project' (Jira) ou 'repos' (GitHub)"
        )

    # repos para validação de título (pode ser subconjunto dos projetos)
    raw_repos = raw.get("repos") or []
    repos = frozenset(str(r).split("/")[-1] for r in raw_repos)
    # Adiciona os repos sem "org/" prefix também
    repos = repos | frozenset(str(r) for r in raw_repos)

    template = raw.get("workflow_template") or "versao-c"

    # workflow_params
    raw_params = raw.get("workflow_params") or {}
    params = WorkflowParams(
        merge_mode=raw_params.get("merge_mode", "manual"),
        review_position=raw_params.get("review_position", "before_qa"),
        deploy_hml_mode=raw_params.get("deploy_hml_mode", "manual"),
        allow_hml_bypass=bool(raw_params.get("allow_hml_bypass", True)),
    )

    # routing
    routing: list[RoutingRule] = []
    default_workflow = "feature-flow"
    for entry in (raw.get("routing") or []):
        if not isinstance(entry, dict):
            continue
        if "default" in entry:
            default_workflow = str(entry["default"])
        elif "match" in entry and "workflow" in entry:
            match = entry["match"]
            if isinstance(match, dict) and "labels" in match:
                label_list = match["labels"]
                if isinstance(label_list, list):
                    routing.append(RoutingRule(
                        labels=frozenset(str(lb) for lb in label_list),
                        workflow=str(entry["workflow"]),
                    ))

    return SquadConfig(
        id=squad_id,
        name=name,
        issue_provider=provider,
        projects=projects,
        repos=repos,
        workflow_template=template,
        workflow_params=params,
        routing=routing,
        default_workflow=default_workflow,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require(raw: dict, key: str, source: str) -> None:
    if not raw.get(key):
        raise SquadConfigError(f"{source}: campo obrigatório ausente: {key!r}")


def _str(raw: dict, key: str, source: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip():
        raise SquadConfigError(f"{source}: {key!r} deve ser uma string não-vazia")
    return v.strip()


def _read_yaml(path: Path) -> dict[str, Any]:
    """Lê um arquivo YAML. Usa PyYAML se disponível, senão fallback."""
    if not path.exists():
        raise SquadConfigError(f"arquivo não encontrado: {path}")
    try:
        import yaml  # type: ignore[import-untyped]
        with path.open() as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return _mini_yaml(path)


def _mini_yaml(path: Path) -> dict[str, Any]:
    """Parser YAML minimalista (fallback sem PyYAML).

    Suporta: escalares, listas (`- item`), mapeamentos aninhados de 1 nível.
    """
    cfg: dict[str, Any] = {}
    cur_key: str | None = None

    def _coerce(val: str) -> Any:
        if val.lower() in ("true", "false"):
            return val.lower() == "true"
        if val.isdigit():
            return int(val)
        return val

    with path.open() as f:
        for raw in f:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indented = line.startswith((" ", "\t"))
            stripped = line.strip()

            if stripped.startswith("- ") and cur_key:
                item = stripped[2:].strip().strip("\"'")
                # Tenta parsear como dict inline: {key: val}
                if item.startswith("{") and item.endswith("}"):
                    inner = item[1:-1].strip()
                    entry: dict[str, Any] = {}
                    for raw_part in inner.split(","):
                        part = raw_part.strip()
                        if ": " in part:
                            k, _, v = part.partition(": ")
                            k = k.strip().strip("\"'")
                            v = v.strip().strip("\"'")
                            if v.startswith("[") and v.endswith("]"):
                                entry[k] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
                            else:
                                entry[k] = _coerce(v)
                    if isinstance(cfg.get(cur_key), list):
                        cfg[cur_key].append(entry)
                elif isinstance(cfg.get(cur_key), list):
                    cfg[cur_key].append(item)
                continue

            if indented and ":" in stripped and cur_key:
                if not isinstance(cfg.get(cur_key), dict):
                    if cfg.get(cur_key):
                        continue
                    cfg[cur_key] = {}
                k, _, v = stripped.partition(":")
                v_stripped = v.strip().strip("\"'")
                if v_stripped:
                    cfg[cur_key][k.strip().strip("\"'")] = _coerce(v_stripped)
                continue

            if ":" in line and not indented:
                key, _, val = line.partition(":")
                key, raw_val = key.strip(), val.strip()
                val_stripped = raw_val.strip("\"'")
                quoted_empty = val_stripped == "" and raw_val in ('""', "''")
                if val_stripped == "" and not quoted_empty:
                    cfg[key], cur_key = [], key
                else:
                    cur_key = None
                    cfg[key] = "" if quoted_empty else _coerce(val_stripped)
    return cfg
