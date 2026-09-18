"""Executor da Fase 1 — lógica de decisão por template.

Recebe um ScanResult e retorna uma ExecutorDecision.
Não executa I/O — apenas decide.

Os 4 templates fixos da Fase 1:
  feature — fluxo completo (Versão C)
  bug     — investigação shift-left na entrada
  hotfix  — fluxo comprimido, bypass auditado
  debt    — autoridade TL, pré-condição COV

O executor não é um `if` sobre labels — ele INTERPRETA o template.
Cada template tem comportamentos diferentes no MESMO motor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Tipos de ação
# ---------------------------------------------------------------------------

class ActionKind(StrEnum):
    DISPATCH_DEV   = "dispatch_dev"      # dispara sessão one-shot de implementação
    DISPATCH_REVIEWER = "dispatch_reviewer"  # dispara kiro-reviewer
    NOTIFY_HUMAN   = "notify_human"      # avisa humano (TL, QA, Dev)
    BLOCK          = "block"             # marca crewflow:blocked + motivo
    REBRAND        = "rebrand"           # troca de template (GATE 0 do hotfix)
    MERGE_PR       = "merge_pr"          # merge squash automático (reviewer aprovado, zero comentários)
    SKIP           = "skip"              # nada a fazer neste ciclo


class HumanRole(StrEnum):
    TL  = "tl"   # tech lead
    DEV = "dev"  # desenvolvedor
    QA  = "qa"   # QA


# ---------------------------------------------------------------------------
# Decisão do executor
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExecutorDecision:
    """O que o executor decidiu fazer com esta issue.

    Campos:
      action       — qual ação tomar (ver ActionKind)
      reason       — motivo legível (vai para o comentário de auditoria #8)
      notify_role  — para NOTIFY_HUMAN: quem deve agir
      block_reason — para BLOCK: motivo detalhado
      new_template — para REBRAND: o template para o qual trocar
      add_labels   — labels a adicionar após a ação
      remove_labels— labels a remover após a ação
    """

    action: ActionKind
    reason: str
    notify_role: HumanRole | None = None
    block_reason: str = ""
    new_template: str | None = None
    add_labels: tuple[str, ...] = field(default_factory=tuple)
    remove_labels: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Routing de template (fallback quando squad não está disponível)
# ---------------------------------------------------------------------------

def _detect_template(labels: frozenset[str]) -> str:
    """Routing de template pelas labels — usado como fallback quando squad=None.

    Quando uma SquadConfig é passada para decide(), usa squad.resolve_workflow()
    que é declarativo e configurável. Este fallback mantém compatibilidade
    com chamadas que não têm squad disponível.
    """
    if "crewflow:hotfix" in labels:
        return "hotfix"
    if "crewflow:bug" in labels:
        return "bug"
    if "crewflow:debt" in labels:
        return "debt"
    return "feature"


def resolve_template(scan_result: object, squad: object | None = None) -> str:
    """Resolve o template de uma issue com o MESMO routing que decide() usa.

    Fonte única de verdade do routing de template: usa squad.resolve_workflow()
    quando uma SquadConfig é fornecida (declarativo/configurável) e cai no
    fallback por labels caso contrário. Exposto para que chamadores (ex.: o
    driving adapter em deployment.run()) possam logar/inspecionar o template
    resolvido pelo executor sem re-implementar a lógica e arriscar drift.
    """
    from flow.scan.scanner import ScanResult

    r: ScanResult = scan_result  # type: ignore[assignment]
    labels = r.item.labels

    if squad is not None:
        from flow.config.squad import SquadConfig
        sq: SquadConfig = squad  # type: ignore[assignment]
        # Normaliza "*-flow" para o nome curto que o executor usa internamente
        return sq.resolve_workflow(labels).replace("-flow", "")
    return _detect_template(labels)


# ---------------------------------------------------------------------------
# Executor principal
# ---------------------------------------------------------------------------

def decide(
    scan_result: object,
    state_comment: str | None = None,
    squad: object | None = None,
    pr_head_sha: str | None = None,
) -> ExecutorDecision:
    """Decide o que fazer com a issue.

    Args:
        scan_result:   ScanResult do scan
        state_comment: conteúdo do <!-- KIRO-FLOW-STATE --> se existir
        squad:         SquadConfig da squad (opcional); quando fornecido,
                       usa squad.resolve_workflow() para routing — mais
                       preciso e configurável que o fallback por labels.
        pr_head_sha:   SHA do HEAD atual do PR associado à issue (opcional).
                       Quando fornecido, é comparado com o SHA registrado
                       no ReviewerResult para detectar push pós-review.
                       Deve ser obtido pelo driving adapter via
                       ``get_pr_for_issue()`` e passado aqui — o executor
                       não faz I/O.

    Returns:
        ExecutorDecision com a ação e os metadados para o executor de I/O.
    """
    from flow.domain import gates
    from flow.domain.state import Modifier, State
    from flow.scan.scanner import ScanResult

    r: ScanResult = scan_result  # type: ignore[assignment]
    item = r.item
    current_state = r.current_state
    modifiers = r.modifiers

    # Nada a fazer se não há estado ou não é candidato
    if current_state is None:
        return ExecutorDecision(action=ActionKind.SKIP, reason="issue fora da esteira")

    # Routing: usa squad.resolve_workflow() se disponível, fallback por labels.
    # Fonte única em resolve_template() — reusada por chamadores externos.
    template = resolve_template(r, squad)

    # ── GATE 0 do hotfix: triagem ──────────────────────────────────────
    if template == "hotfix":
        verdict = gates.triage_hotfix(item)
        if verdict.result.failed:
            if verdict.switch is not None:
                new_t = verdict.switch.value  # "bug" ou "feature"
                return ExecutorDecision(
                    action=ActionKind.REBRAND,
                    reason=f"GATE 0: {verdict.result.reason}",
                    new_template=new_t,
                    add_labels=(f"crewflow:{new_t}",),
                    remove_labels=("crewflow:hotfix",),
                )
            return ExecutorDecision(
                action=ActionKind.NOTIFY_HUMAN,
                reason=f"GATE 0 pendente: {verdict.result.reason}",
                notify_role=HumanRole.TL,
            )

    # ── GATE de entrada do débito técnico ──────────────────────────────
    if template == "debt" and current_state is State.TODO:
        # Verifica se o TL já aprovou via comentário de estado
        from flow.audit.state_comment import parse as _parse_comment
        sc = _parse_comment(state_comment) if state_comment else None
        tl_approved = sc is not None and sc.has_approval("gate-tl")

        if not tl_approved:
            return ExecutorDecision(
                action=ActionKind.NOTIFY_HUMAN,
                reason=(
                    "GATE DT: aguardando aprovação do TL (autoridade técnica, não PM). "
                    "Para aprovar: adicione um comentário na issue com o marcador "
                    "<!-- KIRO-FLOW-STATE --> contendo uma linha "
                    "| `gate-tl` | @seu-usuario | data | na seção '### Aprovações'."
                ),
                notify_role=HumanRole.TL,
            )

    # ── Lock anti-loop: crewflow:reviewed ─────────────────────────────
    # Se reviewed está presente, lemos o resultado do reviewer no state_comment.
    # - SHA divergiu (push pós-review) → remove reviewed e redespacha reviewer
    # - Aprovado sem comentários → MERGE_PR (caminho feliz)
    # - Aprovado com comentários → NOTIFY_HUMAN TL
    # - Resultado ainda não disponível (reviewer ainda rodando) → SKIP
    if current_state is State.REVIEW and Modifier.REVIEWED in modifiers:
        from flow.audit.state_comment import get_reviewer_result_from_comment
        reviewer_result = get_reviewer_result_from_comment(state_comment)

        if reviewer_result is None:
            # Reviewer ainda não postou resultado — aguardar
            return ExecutorDecision(
                action=ActionKind.SKIP,
                reason="crewflow:reviewed presente mas resultado do reviewer ainda não disponível — aguardando",
            )

        # Verifica se houve push após a review: SHA do PR HEAD vs SHA do reviewer.
        # pr_head_sha é fornecido pelo driving adapter (sem I/O aqui).
        if (
            reviewer_result.sha
            and pr_head_sha
            and pr_head_sha[:8] != reviewer_result.sha[:8]
        ):
            return ExecutorDecision(
                action=ActionKind.DISPATCH_REVIEWER,
                reason=(
                    f"SHA divergiu após review: PR HEAD={pr_head_sha[:8]} "
                    f"vs reviewer SHA={reviewer_result.sha[:8]} — re-revisão necessária"
                ),
                add_labels=("crewflow:reviewed",),
                remove_labels=("crewflow:reviewed",),
            )

        if reviewer_result.is_auto_mergeable:
            return ExecutorDecision(
                action=ActionKind.MERGE_PR,
                reason="reviewer aprovado sem pedidos de mudança — merge squash automático",
                add_labels=("crewflow:done",),
                remove_labels=("crewflow:review", "crewflow:reviewed"),
            )

        # Reviewer tem comentários — notifica TL com o conteúdo do review
        comments_text = "; ".join(reviewer_result.comments) if reviewer_result.comments else "(ver comentário na issue)"
        return ExecutorDecision(
            action=ActionKind.NOTIFY_HUMAN,
            reason=f"reviewer retornou pedidos de mudança: {comments_text}",
            notify_role=HumanRole.TL,
        )

    # ── Pré-condição COV (débito técnico em dev) ───────────────────────
    if template == "debt" and current_state is State.DEV:
        # Verifica se há teste de equivalência — Fase 1: verifica via estado
        # Em Fase 2: o executor vai consultar o CI ou o comentário de estado
        cov_result = gates.has_equivalence_test(item, test_exists=_has_equivalence_test_signal(state_comment))
        if cov_result.failed:
            return ExecutorDecision(
                action=ActionKind.NOTIFY_HUMAN,
                reason=f"PRÉ-CONDIÇÃO COV: {cov_result.reason}",
                notify_role=HumanRole.DEV,
            )

    # ── Bypass do HML: bloqueia merge sem justificativa ───────────────
    if Modifier.HML_BYPASS in modifiers:
        justification = _extract_justification(state_comment)
        bypass_result = gates.validate_hml_bypass(item, justification)
        if bypass_result.failed:
            return ExecutorDecision(
                action=ActionKind.BLOCK,
                reason="crewflow:hml-bypass sem justificativa",
                block_reason=bypass_result.reason,
            )

    # ── Ações por estado ───────────────────────────────────────────────
    return _decide_by_state(current_state, template, r)


def _decide_by_state(
    current_state: object,
    template: str,
    r: object,
) -> ExecutorDecision:
    """Decide a ação com base no estado atual da issue."""
    from flow.domain.state import State

    s: State = current_state  # type: ignore[assignment]

    if s is State.TODO:
        return ExecutorDecision(
            action=ActionKind.DISPATCH_DEV,
            reason=f"template {template}: issue pronta para implementação",
            add_labels=("crewflow:dev", "crewflow:running"),
            remove_labels=("crewflow:todo",),
        )

    if s is State.REVIEW:
        # kiro-reviewer: dispara análise automatizada de code review
        return ExecutorDecision(
            action=ActionKind.DISPATCH_REVIEWER,
            reason="issue em review: disparando análise automatizada de code review",
            add_labels=("crewflow:reviewed",),
        )

    if s is State.DEV:
        # Ainda em dev — aguarda o agente de implementação terminar
        return ExecutorDecision(
            action=ActionKind.SKIP,
            reason="em desenvolvimento; aguardando sessão one-shot encerrar",
        )

    if s is State.QA:
        # Avisa Dev para fazer o deploy HML se necessário
        # (em Fase 1, o aviso é informativo — o Dev decide)
        return ExecutorDecision(
            action=ActionKind.NOTIFY_HUMAN,
            reason="crewflow:qa: aguardando validação em HML pelo QA",
            notify_role=HumanRole.QA,
        )

    if s is State.SPEC:
        # Avisa PM/TL que a spec precisa de atenção
        return ExecutorDecision(
            action=ActionKind.NOTIFY_HUMAN,
            reason="crewflow:spec: aguardando aprovação da spec pelo TL",
            notify_role=HumanRole.TL,
        )

    if s is State.READY:
        return ExecutorDecision(
            action=ActionKind.NOTIFY_HUMAN,
            reason="crewflow:ready: aguardando priorização",
            notify_role=HumanRole.TL,
        )

    if s is State.DONE:
        return ExecutorDecision(
            action=ActionKind.SKIP,
            reason="crewflow:done: concluída",
        )

    # Estado desconhecido
    return ExecutorDecision(
        action=ActionKind.SKIP,
        reason=f"estado desconhecido: {s}",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_equivalence_test_signal(state_comment: str | None) -> bool:
    """Verifica se o comentário de estado sinaliza teste de equivalência."""
    from flow.audit.state_comment import has_equivalence_test_signal
    return has_equivalence_test_signal(state_comment)


def _extract_justification(state_comment: str | None) -> str | None:
    """Extrai a justificativa de bypass do comentário de estado."""
    from flow.audit.state_comment import extract_bypass_justification
    return extract_bypass_justification(state_comment)
