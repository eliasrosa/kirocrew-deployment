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
    auto_merge_on_approve: bool = False  # merge automático após approve (opt-in)


@dataclass(frozen=True, slots=True)
class RepoConfig:
    """Configuração específica por repositório.

    Permite sobrepor flags globais (auto_dispatch, auto_merge) para um repo
    individual. Campos ``None`` significam "usar o valor global como fallback".

    Exemplo no squad YAML::

        repos_config:
          - name: org/api-gateway2
            auto_dispatch: true
            auto_merge: false   # merge manual neste repo
    """

    name: str
    auto_dispatch: bool | None = None  # None = fallback ao global
    auto_merge: bool | None = None     # None = fallback ao global


# Além do bloco ``repos_config:`` (issue #245), os flags por repo também podem
# ser declarados INLINE sob a chave ``repos:`` (issue #264), onde cada item
# pode ser um mapa com ``url`` / ``auto_dispatch`` / ``auto_merge``. Exemplo::
#
#     repos:
#       - url: https://github.com/eliasrosa/kirocrew-flow
#         auto_dispatch: true
#         auto_merge: true
#       - url: https://dev.azure.com/your-org/.../your-repo
#         auto_dispatch: true
#         auto_merge: false   # merge manual em PRD
#
# Precedência quando o mesmo repo aparece nas DUAS fontes: a entrada inline em
# ``repos:`` vence a de ``repos_config:`` — ela fica mais próxima da definição
# do repo e é a forma recomendada pela issue #264.


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
    repo_configs: list[RepoConfig] = field(default_factory=list)  # config por repo
    # Defaults globais (issue #264): usados como fallback pelos métodos
    # auto_dispatch(repo_url)/auto_merge(repo_url), que resolvem o global
    # internamente. A camada de deployment popula estes campos a partir do
    # deployment.config.yaml (auto_dispatch global e auto_merge_on_approve).
    global_auto_dispatch: bool = False
    global_auto_merge: bool = False

    def resolve_workflow(self, labels: frozenset[str]) -> str:
        """Retorna o nome do workflow para um conjunto de labels.

        Avalia as regras em ordem; retorna o default se nenhuma casar.
        """
        for rule in self.routing:
            if rule.labels <= labels:  # todas as labels da regra estão presentes
                return rule.workflow
        return self.default_workflow

    def get_repo_config(self, repo: str) -> RepoConfig | None:
        """Retorna a RepoConfig para o repo, ou None se não houver config específica.

        A busca é feita em duas passadas para evitar colisões entre repos que
        compartilham o nome curto em orgs/hosts diferentes (ex.: ``orgA/service``
        vs ``orgB/service``, ou um repo GitHub ``.../service`` vs um Azure
        ``.../_git/service``) — cenário real quando a squad mistura GitHub e
        Azure DevOps (issue #264):

        1. Casamento pelo identificador normalizado completo (host/org/repo):
           preciso quando ambos os lados carregam contexto de org/host.
        2. Só então, como compatibilidade retroativa (issue #245), casamento
           pelo nome curto (último segmento do path).

        Assim, quando o chamador passa um identificador com org/host, uma
        entrada de outro org com o mesmo nome curto NÃO é retornada por engano.
        """
        repo_norm = _normalize_repo_identifier(repo)
        # Passada 1: identificador normalizado completo.
        for rc in self.repo_configs:
            if _normalize_repo_identifier(rc.name) == repo_norm or rc.name == repo:
                return rc
        # Passada 2: fallback por nome curto (compat #245). Só aplica quando pelo
        # menos um dos lados NÃO carrega contexto de org/host — caso contrário
        # dois orgs distintos com o mesmo nome curto colidiriam (issue #264).
        repo_short = repo.split("/")[-1]
        repo_has_org = "/" in repo_norm
        for rc in self.repo_configs:
            rc_norm = _normalize_repo_identifier(rc.name)
            rc_has_org = "/" in rc_norm
            if repo_has_org and rc_has_org:
                # Ambos têm org/host e já falharam no casamento completo (passada
                # 1) → são repos diferentes. Não casa pelo nome curto.
                continue
            if rc.name.split("/")[-1] == repo_short:
                return rc
        return None

    def auto_dispatch_for(self, repo: str, global_auto_dispatch: bool) -> bool:
        """Retorna a flag auto_dispatch efectiva para um repo.

        Prioridade: config por repo > flag global.
        """
        rc = self.get_repo_config(repo)
        if rc is not None and rc.auto_dispatch is not None:
            return rc.auto_dispatch
        return global_auto_dispatch

    def auto_merge_for(self, repo: str, global_auto_merge: bool) -> bool:
        """Retorna a flag auto_merge efectiva para um repo.

        Prioridade: config por repo > flag global (workflow_params.auto_merge_on_approve).
        """
        rc = self.get_repo_config(repo)
        if rc is not None and rc.auto_merge is not None:
            return rc.auto_merge
        return global_auto_merge

    def auto_dispatch(self, repo_url: str) -> bool:
        """Retorna a flag auto_dispatch efectiva para um repo (issue #264).

        Recebe apenas a url/identificador do repo; o default global é
        resolvido internamente a partir de ``global_auto_dispatch``.
        Prioridade: config por repo > global armazenado.
        """
        return self.auto_dispatch_for(repo_url, self.global_auto_dispatch)

    def auto_merge(self, repo_url: str) -> bool:
        """Retorna a flag auto_merge efectiva para um repo (issue #264).

        Recebe apenas a url/identificador do repo; o default global é
        resolvido internamente a partir de ``global_auto_merge``.
        Prioridade: config por repo > global armazenado.
        """
        return self.auto_merge_for(repo_url, self.global_auto_merge)


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

    # Normaliza as entradas de 'repos:': cada item pode ser uma string simples
    # (comportamento legado) OU um mapa com 'url'/'auto_dispatch'/'auto_merge'
    # (issue #264). Extrai a lista de urls/strings e a lista de RepoConfig inline.
    raw_repos = raw.get("repos") or []
    repo_urls, inline_repo_configs = _normalize_repos(raw_repos, source)

    # projetos: campo "project" (Jira) ou "repos" (GitHub) — ou ambos
    projects: list[str] = []
    if raw.get("project"):
        projects.append(str(raw["project"]))
    projects.extend(repo_urls)
    if not projects:
        raise SquadConfigError(
            f"{source}: squad deve ter 'project' (Jira) ou 'repos' (GitHub)"
        )

    # repos para validação de título (pode ser subconjunto dos projetos)
    repos = frozenset(str(r).split("/")[-1] for r in repo_urls)
    # Adiciona os repos sem "org/" prefix também
    repos = repos | frozenset(str(r) for r in repo_urls)

    template = raw.get("workflow_template") or "versao-c"

    # workflow_params
    raw_params = raw.get("workflow_params") or {}
    params = WorkflowParams(
        merge_mode=raw_params.get("merge_mode", "manual"),
        review_position=raw_params.get("review_position", "before_qa"),
        deploy_hml_mode=raw_params.get("deploy_hml_mode", "manual"),
        allow_hml_bypass=bool(raw_params.get("allow_hml_bypass", True)),
        auto_merge_on_approve=bool(raw_params.get("auto_merge_on_approve", False)),
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
        repo_configs=_merge_repo_configs(
            inline_repo_configs,
            _parse_repo_configs(raw.get("repos_config") or []),
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_repo_identifier(repo: str) -> str:
    """Converte a ``url``/string de um repo no identificador que o scanner e os
    providers SCM consomem.

    O scanner (``flow/scan/scanner.py``) itera ``config.projects`` e passa cada
    valor ao provider; o transport do GitHub interpola ``project`` direto em
    ``gh api repos/{owner_repo}/...`` — logo ``project`` PRECISA estar na forma
    ``owner/repo`` (uma url completa produziria ``repos/https://.../...``,
    quebrando a chamada, issue #264). Este helper normaliza:

      - ``https://github.com/eliasrosa/kirocrew-flow`` → ``eliasrosa/kirocrew-flow``
        (também aceita sufixo ``.git`` e barra final);
      - ``https://dev.azure.com/{org}/{project}/_git/{repo}`` →
        ``{org}/{project}/{repo}`` — a forma sem a url que a factory de SCM
        (``scm_config_from_repo_entry`` / ``ScmRepoConfig``) usa como coordenadas
        (azure_org/azure_project/azure_repo);
      - qualquer outra string (ex.: ``owner/repo`` puro, ou uma entrada legada
        sem esquema) passa inalterada — compatibilidade retroativa.

    É idempotente: aplicar de novo a um identificador já normalizado não muda
    nada, o que mantém o casamento de ``get_repo_config`` consistente.
    """
    value = repo.strip()
    low = value.lower()

    if low.startswith("https://github.com/") or low.startswith("http://github.com/"):
        rest = value.split("github.com/", 1)[1]
        rest = rest.strip("/")
        if rest.endswith(".git"):
            rest = rest[: -len(".git")]
        # owner/repo — descarta segmentos extras (ex.: /pull/1) por segurança.
        parts = [p for p in rest.split("/") if p]
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"
        return rest

    if "dev.azure.com/" in low and "/_git/" in low:
        # https://dev.azure.com/{org}/{project}/_git/{repo}
        after_host = value.split("dev.azure.com/", 1)[1]
        org_project, _, repo_part = after_host.partition("/_git/")
        org_project = org_project.strip("/")
        repo_part = repo_part.strip("/").split("/")[0]  # ignora sufixos
        if org_project and repo_part:
            return f"{org_project}/{repo_part}"
        return value

    return value


def _normalize_repos(
    raw_repos: list, source: str
) -> tuple[list[str], list[RepoConfig]]:
    """Normaliza a lista ``repos:`` em (identificadores, configs inline).

    Cada item pode ser:
      - uma string simples (legado) → vira um identificador de repo, sem
        config inline;
      - um mapa com ``url`` (obrigatório) e, opcionalmente, ``auto_dispatch``
        e ``auto_merge`` (issue #264) → a ``url`` é NORMALIZADA para o
        identificador que o scanner/provider espera (ver
        ``_normalize_repo_identifier``) e alimenta ``projects``/``repos``
        (como uma string faria); os flags viram um RepoConfig inline.

    Aceita url completa (github.com, dev.azure.com) ou o formato ``owner/repo``
    puro — ambos resultam no mesmo identificador canônico, de modo que a url
    NUNCA vaza para ``projects`` (o que quebraria as chamadas ``gh api``).
    """
    identifiers: list[str] = []
    inline: list[RepoConfig] = []
    for entry in raw_repos:
        if isinstance(entry, dict):
            url = entry.get("url")
            if not url or not isinstance(url, str):
                raise SquadConfigError(
                    f"{source}: entrada de 'repos' em forma de mapa exige 'url' string: {entry!r}"
                )
            identifier = _normalize_repo_identifier(url)
            identifiers.append(identifier)
            auto_dispatch: bool | None = None
            if "auto_dispatch" in entry:
                auto_dispatch = bool(entry["auto_dispatch"])
            auto_merge: bool | None = None
            if "auto_merge" in entry:
                auto_merge = bool(entry["auto_merge"])
            if auto_dispatch is not None or auto_merge is not None:
                inline.append(
                    RepoConfig(
                        name=identifier,
                        auto_dispatch=auto_dispatch,
                        auto_merge=auto_merge,
                    )
                )
        else:
            identifiers.append(_normalize_repo_identifier(str(entry)))
    return identifiers, inline


def _merge_repo_configs(
    inline: list[RepoConfig], legacy: list[RepoConfig]
) -> list[RepoConfig]:
    """Mescla configs inline (``repos:``) e legadas (``repos_config:``).

    Precedência: quando o mesmo repo aparece nas duas fontes, a entrada inline
    de ``repos:`` vence (ver docstring de RepoConfig).

    Deduplicação: casamos primeiro pelo identificador NORMALIZADO completo
    (host/org/repo — ver ``_normalize_repo_identifier``) e só usamos o nome
    curto como fallback quando o identificador não carrega org/host. Isso evita
    que ``orgA/service`` e ``orgB/service`` (ou um GitHub ``.../service`` e um
    Azure ``.../_git/service``) sejam colapsados por engano — colisão real numa
    squad que mistura GitHub e Azure DevOps (issue #264).
    """
    def _short(name: str) -> str:
        return name.split("/")[-1]

    def _has_org_context(name: str) -> bool:
        # Um identificador com org/host tem ao menos um "/" (owner/repo,
        # org/project/repo, ...). Nomes curtos "soltos" não têm.
        return "/" in _normalize_repo_identifier(name)

    merged: list[RepoConfig] = list(inline)
    seen_full = {_normalize_repo_identifier(rc.name) for rc in inline}
    # Nomes curtos só bloqueiam quando a entrada inline NÃO tem org/host — caso
    # contrário confiamos no identificador completo para não colapsar orgs.
    seen_short = {_short(rc.name) for rc in inline if not _has_org_context(rc.name)}
    for rc in legacy:
        full = _normalize_repo_identifier(rc.name)
        if full in seen_full:
            continue
        if not _has_org_context(rc.name) and _short(rc.name) in seen_short:
            continue
        merged.append(rc)
        seen_full.add(full)
        if not _has_org_context(rc.name):
            seen_short.add(_short(rc.name))
    return merged


def _parse_repo_configs(raw_list: list) -> list[RepoConfig]:
    """Parseia a lista repos_config do YAML em objetos RepoConfig."""
    configs: list[RepoConfig] = []
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name or not isinstance(name, str):
            continue
        auto_dispatch: bool | None = None
        if "auto_dispatch" in entry:
            auto_dispatch = bool(entry["auto_dispatch"])
        auto_merge: bool | None = None
        if "auto_merge" in entry:
            auto_merge = bool(entry["auto_merge"])
        configs.append(RepoConfig(name=name.strip(), auto_dispatch=auto_dispatch, auto_merge=auto_merge))
    return configs


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
