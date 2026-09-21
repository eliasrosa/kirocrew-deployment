"""Scanner zero-token — lógica principal.

Fluxo por ciclo:
1. Para cada projeto configurado na squad, lista issues com labels crewflow:*
2. Para cada issue, computa o hash atual das labels
3. Compara com o hash armazenado no cache SQLite
4. Se mudou (ou é novo): avalia se é candidato a dispatch
5. Atualiza o cache
6. Retorna apenas os candidatos reais

Nenhum token de agente é gasto neste módulo.

Shadow mode (fase #177-A):
  ``implicit_state()`` deriva o estado esperado de uma issue com base em
  evidências externas (branch existente, PR aberta, PR aprovada, issue fechada)
  em paralelo com as labels existentes.  Hoje é só para log — as labels
  continuam sendo a fonte de verdade para o executor.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from flow.domain import gates
from flow.domain import state as state_mod
from flow.domain.gates import Squad, WorkItem
from flow.domain.state import Modifier, State, is_dispatchable, parse_modifiers, parse_state
from flow.ports.issue_provider import IssueProvider, ProviderError
from flow.scan.cache import compute_hash, get_hash, set_hash

# Estados que o executor precisa monitorar ativamente em todo ciclo, mesmo sem
# mudança de labels.  O cache filtra issues inativas com zero custo, mas issues
# nestes estados precisam aparecer no scan para que o motor possa tomar ação
# (ex: redisparar o reviewer em REVIEW_WAITING, avisar QA em QA_WAITING).
ALWAYS_INCLUDE_STATES: frozenset[State] = frozenset({State.REVIEW_WAITING, State.QA_WAITING})

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Estado implícito (shadow mode)
# ---------------------------------------------------------------------------

class ImplicitState(StrEnum):
    """Estado implícito derivado de evidências externas (branch + PR + issue state).

    Não substitui as labels — é derivado em paralelo para validação (shadow mode).

    Mapeamento das evidências para o estado esperado:
        done       — issue fechada
        review_ok  — PR aprovada (review decisions incluem "APPROVED")
        review     — PR aberta (qualquer estado de mergeable)
        dev        — branch feat/issue-N existe mas sem PR aberta
        todo       — sem branch e sem PR
    """

    TODO      = "todo"       # sem branch e sem PR aberta
    DEV       = "dev"        # branch existe, sem PR aberta
    REVIEW    = "review"     # PR aberta
    REVIEW_OK = "review_ok"  # PR aprovada (pelo menos 1 aprovação)
    DONE      = "done"       # issue fechada


def implicit_state(
    issue_closed: bool,
    branches: list[str],
    prs: list[dict],
    issue_number: int | str | None = None,
) -> ImplicitState:
    """Deriva o estado implícito de uma issue com base em evidências externas.

    Puro Python, sem I/O.  O chamador é responsável por buscar os dados e
    passar como parâmetros — esta função não faz chamadas de rede.

    Args:
        issue_closed:  True se a issue está fechada no provedor.
        branches:      Lista de nomes de branches que existem no repositório
                       para esta issue.  O nome canônico é ``feat/issue-N``,
                       mas outros prefixos são aceitos para robustez.
        prs:           Lista de PRs abertas associadas à issue.  Cada entry é
                       um dict com pelo menos:
                           - ``state``: ``"open"`` | ``"closed"`` | ``"merged"``
                           - ``reviews``: lista de dicts com ``state`` do reviewer
                             (``"APPROVED"``, ``"CHANGES_REQUESTED"``, etc.) —
                             pode estar ausente ou vazia.
        issue_number:  Número da issue (usado para filtrar branch canônica).
                       Opcional — sem ele, qualquer branch passada conta.

    Returns:
        O ``ImplicitState`` mais avançado que as evidências suportam.
    """
    # Prioridade decrescente: done > review_ok > review > dev > todo

    if issue_closed:
        return ImplicitState.DONE

    # Filtra só PRs abertas
    open_prs = [pr for pr in prs if (pr.get("state") or "").lower() == "open"]

    if open_prs:
        # Verifica se alguma PR aberta tem pelo menos 1 aprovação
        for pr in open_prs:
            reviews = pr.get("reviews") or []
            approved = any(
                (r.get("state") or "").upper() == "APPROVED"
                for r in reviews
            )
            if approved:
                return ImplicitState.REVIEW_OK
        return ImplicitState.REVIEW

    # Verifica se existe branch para a issue
    has_branch = bool(branches)
    if has_branch and issue_number is not None:
        canonical = f"feat/issue-{issue_number}"
        has_branch = canonical in branches

    if has_branch:
        return ImplicitState.DEV

    return ImplicitState.TODO


# ---------------------------------------------------------------------------
# Configuração da squad (simplificada para o scan)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SquadScanConfig:
    """Configuração mínima que o scan precisa — sem worktrees, sem crons.

    ``issue_provider`` = "github" | "jira"
    ``projects``       = lista de projetos a varrer (chave Jira ou owner/repo)
    ``repos``          = lista de repos conhecidos (para validação de título)
    """

    squad_id: str
    issue_provider: str
    projects: tuple[str, ...]
    repos: frozenset[str]
    data_dir: str | None = None  # None = usa o default ~/.kiro/crew/kirocrew-flow


# ---------------------------------------------------------------------------
# Resultado do scan
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScanResult:
    """Uma issue candidata a processamento neste ciclo.

    ``dispatch_candidate`` = True se está em crewflow:todo sem modificadores de parada.
    ``spec_valid``         = True se o título tem um repo reconhecido (GATE 1 zero-token).
    ``changed``            = True se as labels mudaram desde o último ciclo.
    ``reason``             = motivo de estar neste resultado (para log).
    ``implicit_state``     = estado derivado de evidências externas (branch/PR/issue state).
                             None quando o chamador não forneceu os dados externos.
    """

    item: WorkItem
    current_state: State | None
    modifiers: frozenset[Modifier]
    dispatch_candidate: bool
    spec_valid: bool | None  # None = não verificado (estado não é spec)
    changed: bool
    reason: str
    implicit_state: ImplicitState | None = None  # shadow mode — None = dados não fornecidos


# ---------------------------------------------------------------------------
# Scanner principal
# ---------------------------------------------------------------------------

def scan_candidates(
    config: SquadScanConfig,
    provider: IssueProvider,
    conn: sqlite3.Connection,
) -> list[ScanResult]:
    """Varre os projetos da squad e retorna as issues candidatas.

    Zero token gasto: só código Python + chamadas de API.
    O agente só é acordado com os resultados já filtrados.
    """

    squad = Squad(id=config.squad_id, repos=config.repos)
    results: list[ScanResult] = []
    active_keys: set[str] = set()
    # Deduplication por KEY dentro do ciclo: a mesma issue pode aparecer em
    # mais de um projeto da squad (squad GitHub-first com múltiplos repos que
    # compartilham a esteira). Uma issue é uma unidade de trabalho única — sua
    # key a identifica de forma canônica — então ela deve produzir NO MÁXIMO um
    # ScanResult por ciclo, independente de quantos projetos a listem. Sem isso,
    # a segunda ocorrência dependia do hash já gravado no cache pelo primeiro
    # projeto (changed=False), um efeito colateral frágil (issue #43).
    seen_keys: set[str] = set()

    for project in config.projects:
        try:
            project_results = _scan_project(project, provider, conn, squad, seen_keys)
            results.extend(project_results)
            active_keys.update(r.item.key for r in project_results)
        except ProviderError as exc:
            logger.error("scan: erro ao varrer %s: %s", project, exc)
            # Continua para o próximo projeto — falha parcial não para o scan

    logger.info(
        "scan: %d candidatos em %d projetos",
        len([r for r in results if r.dispatch_candidate]),
        len(config.projects),
    )
    return results


def _scan_project(
    project: str,
    provider: IssueProvider,
    conn: sqlite3.Connection,
    squad: Squad,
    seen_keys: set[str],
) -> list[ScanResult]:
    """Varre um único projeto e retorna os resultados.

    ``seen_keys`` é compartilhado entre todos os projetos do ciclo: uma key já
    emitida por um projeto anterior é ignorada aqui (deduplication por key).
    """
    results: list[ScanResult] = []

    # Lista issues com qualquer label crewflow:* de estado
    # Não filtra por estado aqui — o domínio decide o que fazer com cada uma
    all_items = _fetch_all_labeled_items(project, provider)

    logger.debug("scan: %d issues com labels crewflow:* em %s", len(all_items), project)

    for raw_item in all_items:
        key = raw_item.get("key", "")
        if key and key in seen_keys:
            # Mesma issue já avaliada em outro projeto neste ciclo — deduplica
            # por key em vez de depender do hash gravado no cache (issue #43).
            logger.debug(
                "scan: issue %s deduplicada em %s (já vista em outro projeto do ciclo)",
                key,
                project,
            )
            continue

        result = _evaluate_item(raw_item, project, provider, conn, squad)
        if result is not None:
            seen_keys.add(result.item.key)
            results.append(result)

    return results


def _fetch_all_labeled_items(project: str, provider: IssueProvider) -> list[dict]:
    """Lista todas as issues com labels crewflow:* (todos os estados)."""
    all_items: list[dict] = []
    seen: set[str] = set()

    # Busca por cada estado — a API filtra por uma label por vez
    for s in State:
        try:
            items = provider.list_by_state(project, s.value)
            for item in items:
                key = item.get("key", "")
                if key and key not in seen:
                    seen.add(key)
                    all_items.append(item)
        except ProviderError as exc:
            logger.warning("scan: erro ao listar %s em %s: %s", s.value, project, exc)

    return all_items


def _evaluate_item(
    raw: dict,
    project: str,
    provider: IssueProvider,
    conn: sqlite3.Connection,
    squad: Squad,
) -> ScanResult | None:
    """Avalia uma issue e retorna um ScanResult, ou None se sem interesse."""
    key = raw.get("key", "")
    if not key:
        return None

    labels: list[str] = raw.get("labels", [])
    current_hash = compute_hash(labels)
    stored_hash = get_hash(conn, key)
    changed = current_hash != stored_hash

    # Atualiza o cache independente de ser candidato
    set_hash(conn, key, current_hash)

    # Parseia o estado e os modificadores
    label_set = frozenset(labels)
    try:
        current_state = parse_state(label_set)
    except state_mod.EstadoAmbiguo as exc:
        logger.warning("scan: %s tem estados ambíguos: %s", key, exc)
        current_state = None

    modifiers = parse_modifiers(label_set)

    # Issue sem estado crewflow: está fora da esteira
    if current_state is None:
        return None

    # Monta o WorkItem mínimo para as validações de domínio
    item = WorkItem(
        key=key,
        title=raw.get("title", ""),
        labels=label_set,
        parent_key=raw.get("parent_key"),
    )

    # Verifica se é candidato a dispatch
    dispatch_candidate = is_dispatchable(current_state, modifiers)

    # Validação zero-token do GATE 1: só para itens em flow:briefing
    spec_valid: bool | None = None
    if current_state is State.BRIEFING:
        spec_result = gates.can_leave_spec(item, squad)
        spec_valid = spec_result.ok
        if not spec_valid:
            logger.debug("scan: %s spec inválida: %s", key, spec_result.reason)

    # Só inclui no resultado se há algo a fazer:
    # - mudou de estado (hash diferente) e é candidato
    # - ou está em spec com problema (para flagrar sem despachar)
    # - ou é a primeira vez que vemos (changed=True porque stored_hash era None)
    # - ou está em um estado 'ativo' que o executor monitora em todo ciclo
    #   (REVIEW, QA) — mesmo sem mudança de labels, o motor pode precisar agir
    #   (ex: redisparar o reviewer ou avisar QA)
    if not changed and not dispatch_candidate and current_state not in ALWAYS_INCLUDE_STATES:
        return None  # nada mudou e não é candidato — skip

    reason = _build_reason(changed, dispatch_candidate, current_state, modifiers, spec_valid)

    return ScanResult(
        item=item,
        current_state=current_state,
        modifiers=modifiers,
        dispatch_candidate=dispatch_candidate,
        spec_valid=spec_valid,
        changed=changed,
        reason=reason,
    )


def _build_reason(
    changed: bool,
    dispatch_candidate: bool,
    current_state: State | None,
    modifiers: frozenset[Modifier],
    spec_valid: bool | None,
) -> str:
    parts: list[str] = []

    if dispatch_candidate:
        parts.append("CANDIDATO A DISPATCH")
    elif current_state == State.BRIEFING and spec_valid is False:
        parts.append("BRIEFING SEM REPO — flagrada para correção humana")
    elif Modifier.BLOCKED in modifiers:
        parts.append(f"bloqueada em {current_state} (flow:blocked)")
    elif current_state is not None and current_state.value == "flow:develop-running":
        parts.append(f"em andamento em {current_state} (flow:develop-running)")
    else:
        parts.append(f"estado: {current_state}")

    if changed:
        parts.append("labels mudaram")
    else:
        parts.append("novo no cache")

    return "; ".join(parts)
