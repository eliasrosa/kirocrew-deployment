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
    dispatch_prompt_extra: str = ""  # texto adicional appendado ao prompt de dispatch

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
        dispatch_prompt_extra=str(raw.get("dispatch_prompt_extra") or "").strip(),
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


def _coerce_scalar(val: str) -> Any:
    """Converte um escalar textual YAML no tipo Python correspondente.

    Conjunto de escalares suportado pelo fallback (subconjunto do YAML,
    suficiente para o schema documentado em ``squads/example.yaml`` e
    ``config.example.yaml``):

      - ``true`` / ``false`` (case-insensitive) → ``bool``;
      - inteiros positivos sem sinal (``str.isdigit()``) → ``int``;
      - qualquer outra coisa → ``str`` (mantida como texto).

    NÃO cobre floats, inteiros negativos, ``null`` nem escalares numéricos
    entre aspas — para esses casos, instale PyYAML (`pip install -e '.[yaml]'`).
    Nenhum campo do schema atual usa esses tipos, então a divergência é inerte;
    ela existe apenas para manter o fallback pequeno e previsível.
    """
    if val.lower() in ("true", "false"):
        return val.lower() == "true"
    if val.isdigit():
        return int(val)
    return val


def _parse_inline_value(val: str) -> Any:
    """Parseia um valor inline: escalar, lista `[...]` ou mapa `{...}`."""
    if val.startswith("[") and val.endswith("]"):
        return [
            _coerce_scalar(x.strip().strip("\"'"))
            for x in val[1:-1].split(",")
            if x.strip()
        ]
    if val.startswith("{") and val.endswith("}"):
        entry: dict[str, Any] = {}
        for raw_part in val[1:-1].split(","):
            part = raw_part.strip()
            if ":" in part:
                k, _, v = part.partition(":")
                entry[k.strip().strip("\"'")] = _parse_inline_value(v.strip().strip("\"'"))
        return entry
    return _coerce_scalar(val.strip("\"'"))


def _mini_yaml(path: Path) -> dict[str, Any]:
    """Parser YAML minimalista (fallback sem PyYAML).

    Suporta:
      - escalares (string, int, bool) no topo e aninhados;
      - listas simples (`- item`);
      - mapeamentos aninhados por indentação (multi-nível);
      - itens de lista que são dicts, tanto na forma inline
        (`- {labels: ["x"]}`) quanto na forma multi-linha padrão do YAML::

            routing:
              - match:
                  labels:
                    - crewflow:bug
                workflow: bug-flow
              - default: feature-flow

    Produz a mesma estrutura que o PyYAML geraria para o schema de squad,
    de modo que `_parse_squad` constrói as RoutingRule de forma idêntica com
    ou sem PyYAML instalado.

    Para squads com routing complexo, PyYAML continua recomendado
    (`pip install -e '.[yaml]'`); este fallback cobre o schema documentado
    em ``squads/example.yaml`` mas não a especificação YAML completa.
    """
    return _parse_block(_read_lines(path), 0)[0]


def _read_lines(path: Path) -> list[tuple[int, str]]:
    """Lê o arquivo em (indentação, conteúdo) ignorando comentários/vazios."""
    out: list[tuple[int, str]] = []
    with path.open() as f:
        for raw in f:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip(" "))
            out.append((indent, line.strip()))
    return out


def _parse_block(lines: list[tuple[int, str]], start: int) -> tuple[dict[str, Any], int]:
    """Parseia um bloco de mapeamento a partir de `start`.

    Retorna o dict e o índice da primeira linha que não pertence ao bloco
    (indentação menor que a do bloco).
    """
    result: dict[str, Any] = {}
    if start >= len(lines):
        return result, start
    block_indent = lines[start][0]
    i = start
    while i < len(lines):
        indent, content = lines[i]
        if indent < block_indent:
            break
        if content.startswith("- "):
            # Item de lista onde esperávamos uma chave de mapa: input ambíguo
            # (indentação inconsistente). Falha alto em vez de descartar dados.
            raise SquadConfigError(
                f"YAML inválido (fallback): item de lista inesperado onde um "
                f"mapa era esperado: {content!r}"
            )
        key, _, rest = content.partition(":")
        key = key.strip().strip("\"'")
        rest = rest.strip()
        if rest == "":
            # Valor em bloco na(s) próxima(s) linha(s). Em YAML, itens de lista
            # podem estar MAIS indentados que a chave OU na MESMA indentação
            # (forma flush-left, ex.: `repos:` seguido de `- a` na coluna 0).
            value: Any
            if (
                i + 1 < len(lines)
                and lines[i + 1][1].startswith("- ")
                and lines[i + 1][0] >= block_indent
            ):
                value, i = _parse_list(lines, i + 1, lines[i + 1][0])
                result[key] = value
            elif i + 1 < len(lines) and lines[i + 1][0] > block_indent:
                value, i = _parse_block(lines, i + 1)
                result[key] = value
            else:
                result[key] = []
                i += 1
        elif rest in ('""', "''"):
            result[key] = ""
            i += 1
        else:
            result[key] = _parse_inline_value(rest)
            i += 1
    return result, i


def _parse_list(
    lines: list[tuple[int, str]], start: int, list_indent: int
) -> tuple[list[Any], int]:
    """Parseia uma lista de itens `- ...` na indentação `list_indent`."""
    items: list[Any] = []
    i = start
    while i < len(lines):
        indent, content = lines[i]
        if indent < list_indent or not content.startswith("- "):
            break
        body = content[2:].strip()
        if not body:
            i += 1
            continue
        # Em YAML, `chave: valor` exige espaço após o `:` (ou terminar em `:`).
        # `- crewflow:bug` sem espaço é um escalar, não um mapa.
        is_map_item = (
            not body.startswith(("{", "["))
            and (": " in body or body.endswith(":"))
        )
        if is_map_item:
            key, _, rest = body.partition(":")
            # Item é um mapa; a primeira chave está na mesma linha do `-`.
            entry: dict[str, Any] = {}
            key = key.strip().strip("\"'")
            rest = rest.strip()
            if rest == "":
                # Chave com bloco aninhado (mapa ou lista) nas próximas linhas.
                if i + 1 < len(lines) and lines[i + 1][0] > indent:
                    if lines[i + 1][1].startswith("- "):
                        entry[key], i = _parse_list(lines, i + 1, lines[i + 1][0])
                    else:
                        entry[key], i = _parse_block(lines, i + 1)
                else:
                    entry[key] = []
                    i += 1
            else:
                entry[key] = _parse_inline_value(rest)
                i += 1
            # Chaves adicionais do mesmo item de lista. A indentação de
            # continuação NÃO é fixada em `indent + 2`: qualquer chave
            # estritamente mais indentada que o marcador `-` pertence ao
            # mesmo item (regra de block-mapping do YAML). A indentação real
            # é derivada da primeira linha de continuação encontrada.
            while i < len(lines) and lines[i][0] > indent and \
                    not lines[i][1].startswith("- "):
                extra, _, xrest = lines[i][1].partition(":")
                extra = extra.strip().strip("\"'")
                xrest = xrest.strip()
                if xrest == "":
                    if i + 1 < len(lines) and lines[i + 1][0] > lines[i][0]:
                        if lines[i + 1][1].startswith("- "):
                            entry[extra], i = _parse_list(lines, i + 1, lines[i + 1][0])
                        else:
                            entry[extra], i = _parse_block(lines, i + 1)
                    else:
                        entry[extra] = []
                        i += 1
                else:
                    entry[extra] = _parse_inline_value(xrest)
                    i += 1
            items.append(entry)
        else:
            items.append(_parse_inline_value(body.strip("\"'")))
            i += 1
    return items, i
