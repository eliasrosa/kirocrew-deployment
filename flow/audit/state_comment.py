"""Comentário de estado estruturado — modelo, renderizador e parser.

Formato do comentário:

    <!-- KIRO-FLOW-STATE -->
    ## 🤖 KiroCrew Flow — Estado

    **Workflow:** feature (v1)
    **Nó atual:** dev
    **Status:** running
    **Repo:** api-gateway2

    ### Histórico
    | Quando | De → Para | Quem |
    |--------|-----------|------|
    | 2026-09-15 00:02 | start → dev | system |

    ### Exceções
    | Exceção | Justificativa | Quem | Quando |
    |---------|---------------|------|--------|
    | `crewflow:hml-bypass` | Checkout fora do ar | @elias | 2026-09-15 |
    <!-- /KIRO-FLOW-STATE -->

Puro Python — sem I/O, testável sem mock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC

MARKER_OPEN  = "<!-- KIRO-FLOW-STATE -->"
MARKER_CLOSE = "<!-- /KIRO-FLOW-STATE -->"

# Marcadores do comentário de review do PR (sem o bloco completo de estado)
PR_REVIEW_COMMENT_MARKER = "<!-- KIRO-FLOW-REVIEW -->"
PR_REVIEW_COMMENT_CLOSE  = "<!-- /KIRO-FLOW-REVIEW -->"


# ---------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TransitionEntry:
    """Uma linha do histórico de transições."""
    when: str       # ISO 8601, ex: "2026-09-15 00:02"
    from_state: str # ex: "start" ou "crewflow:todo"
    to_state: str   # ex: "crewflow:dev"
    actor: str      # ex: "system", "kiro-dev", "@elias"


@dataclass(frozen=True, slots=True)
class ExceptionEntry:
    """Uma linha da seção de exceções (ex: hml-bypass)."""
    label: str          # ex: "crewflow:hml-bypass"
    justification: str  # motivo registrado
    actor: str          # quem registrou
    when: str           # ISO 8601


@dataclass(frozen=True, slots=True)
class ApprovalEntry:
    """Uma linha da seção de aprovações de gate."""
    gate: str      # ex: "gate-tl", "gate-qa"
    actor: str     # ex: "@elias"
    when: str      # ISO 8601


@dataclass(frozen=True, slots=True)
class ReviewerResult:
    """Resultado estruturado do kiro-reviewer.

    ``approved`` — True se o reviewer não pediu mudanças.
    ``comments`` — lista de pedidos de mudança (strings curtas).
    ``sha``       — SHA do commit do PR no momento da análise (anti-loop).
    ``reviewer``  — identificador do agente que fez a análise.
    """

    approved: bool
    comments: tuple[str, ...]
    sha: str = ""
    reviewer: str = "kiro-reviewer"

    @property
    def is_auto_mergeable(self) -> bool:
        """True se aprovado sem nenhum pedido de mudança (critério zero-comentários)."""
        return self.approved and len(self.comments) == 0


@dataclass(slots=True)
class StateComment:
    """Representação completa do comentário de estado.

    Mutável pra permitir add_transition() e add_exception() in-place
    antes de renderizar.
    """

    workflow: str           # ex: "feature (v1)"
    current_node: str       # ex: "dev"
    status: str             # ex: "running", "waiting", "done"
    repo: str               # ex: "api-gateway2"
    history: list[TransitionEntry] = field(default_factory=list)
    exceptions: list[ExceptionEntry] = field(default_factory=list)
    approvals: list[ApprovalEntry] = field(default_factory=list)
    reviewer_result: ReviewerResult | None = None
    review_iterations: int = 0  # contagem de ciclos review↔dev (re-trabalho pós-review)

    def add_transition(
        self, from_state: str, to_state: str, actor: str, when: str | None = None
    ) -> None:
        """Acrescenta uma entrada no histórico."""
        from datetime import datetime
        ts = when or datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M")
        self.history.append(TransitionEntry(
            when=ts, from_state=from_state, to_state=to_state, actor=actor
        ))

    def add_exception(
        self, label: str, justification: str, actor: str, when: str | None = None
    ) -> None:
        """Acrescenta uma entrada na seção de exceções."""
        from datetime import datetime
        ts = when or datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M")
        self.exceptions.append(ExceptionEntry(
            label=label, justification=justification, actor=actor, when=ts
        ))

    def get_justification(self, label: str) -> str | None:
        """Retorna a justificativa para uma exceção específica, ou None."""
        for exc in self.exceptions:
            if exc.label == label:
                return exc.justification
        return None

    def add_approval(self, gate: str, actor: str, when: str | None = None) -> None:
        """Registra aprovação de um gate (ex: 'gate-tl')."""
        from datetime import datetime
        ts = when or datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M")
        self.approvals.append(ApprovalEntry(gate=gate, actor=actor, when=ts))

    def has_approval(self, gate: str) -> bool:
        """Retorna True se o gate especificado foi aprovado."""
        return any(a.gate == gate for a in self.approvals)

    def set_reviewer_result(
        self,
        approved: bool,
        comments: list[str],
        sha: str = "",
        reviewer: str = "kiro-reviewer",
    ) -> None:
        """Registra o resultado do kiro-reviewer no comentário de estado."""
        self.reviewer_result = ReviewerResult(
            approved=approved,
            comments=tuple(comments),
            sha=sha,
            reviewer=reviewer,
        )

    def get_reviewer_result(self) -> ReviewerResult | None:
        """Retorna o resultado do reviewer, ou None se ainda não analisado."""
        return self.reviewer_result


# ---------------------------------------------------------------------------
# Renderizador
# ---------------------------------------------------------------------------

def render(sc: StateComment) -> str:
    """Gera o texto do comentário de estado."""
    lines: list[str] = [
        MARKER_OPEN,
        "## 🤖 KiroCrew Flow — Estado",
        "",
        f"**Workflow:** {sc.workflow}",
        f"**Nó atual:** {sc.current_node}",
        f"**Status:** {sc.status}",
        f"**Repo:** {sc.repo}",
    ]
    if sc.review_iterations > 0:
        lines.append(f"**Iterações de review:** {sc.review_iterations}")
    lines.append("")

    if sc.history:
        lines += [
            "### Histórico",
            "| Quando | De → Para | Quem |",
            "|--------|-----------|------|",
        ]
        for entry in sc.history:
            lines.append(f"| {entry.when} | {entry.from_state} → {entry.to_state} | {entry.actor} |")
        lines.append("")

    if sc.exceptions:
        lines += [
            "### Exceções",
            "| Exceção | Justificativa | Quem | Quando |",
            "|---------|---------------|------|--------|",
        ]
        for exc in sc.exceptions:
            lines.append(
                f"| `{exc.label}` | {exc.justification} | {exc.actor} | {exc.when} |"
            )
        lines.append("")

    if sc.approvals:
        lines += [
            "### Aprovações",
            "| Gate | Aprovado por | Quando |",
            "|------|-------------|--------|",
        ]
        for apv in sc.approvals:
            lines.append(f"| `{apv.gate}` | {apv.actor} | {apv.when} |")
        lines.append("")

    if sc.reviewer_result is not None:
        rr = sc.reviewer_result
        status_str = "✅ aprovado" if rr.approved else "❌ com pedidos de mudança"
        lines += [
            "### Resultado do Reviewer",
            f"**Status:** {status_str}",
            f"**Reviewer:** {rr.reviewer}",
            f"**SHA:** {rr.sha}" if rr.sha else "",
            f"**Auto-merge:** {'sim' if rr.is_auto_mergeable else 'não'}",
        ]
        # Remove linha vazia do SHA quando sha é vazio
        lines = [line for line in lines if line != ""]
        if rr.comments:
            lines.append("")
            lines.append("**Pedidos de mudança:**")
            for c in rr.comments:
                lines.append(f"- {c}")
        lines.append("")

    lines.append(MARKER_CLOSE)
    return "\n".join(lines)


def render_issue_pr_reference(pr_number: int, approved: bool) -> str:
    """Renderiza a referência curta a ser mantida NA ISSUE (link + status).

    Formato (conforme issue #75):

        Review postado em PR #<pr_number> — status: aprovado
                                            (ou pedidos de mudança)

    Não duplica o detalhe dos pedidos de mudança (sem bullets),
    apenas aponta para o PR e resume o status.

    Puro — sem I/O.
    """
    status = "aprovado" if approved else "pedidos de mudança"
    return f"Review postado em PR #{pr_number} — status: {status}"


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse(comment_body: str) -> StateComment | None:
    """Extrai o StateComment de um texto de comentário.

    Retorna None se o marcador não for encontrado.
    """
    if MARKER_OPEN not in comment_body:
        return None

    # Extrai o bloco entre os marcadores
    pattern = re.compile(
        re.escape(MARKER_OPEN) + r"(.*?)" + re.escape(MARKER_CLOSE),
        re.DOTALL,
    )
    m = pattern.search(comment_body)
    if not m:
        return None

    block = m.group(1)
    return _parse_block(block)


def _parse_block(block: str) -> StateComment:
    """Parseia o conteúdo entre os marcadores."""
    lines = block.splitlines()

    workflow = _extract_field(lines, "Workflow")
    current_node = _extract_field(lines, "Nó atual")
    status = _extract_field(lines, "Status")
    repo = _extract_field(lines, "Repo")
    review_iterations_str = _extract_field(lines, "Iterações de review")

    sc = StateComment(
        workflow=workflow or "",
        current_node=current_node or "",
        status=status or "",
        repo=repo or "",
        review_iterations=int(review_iterations_str) if review_iterations_str and review_iterations_str.isdigit() else 0,
    )

    # Parseia histórico
    in_history = False
    for line in lines:
        if "### Histórico" in line:
            in_history = True
            continue
        if in_history:
            if line.startswith("###") and "Histórico" not in line:
                in_history = False
                continue
            t_entry = _parse_history_row(line)
            if t_entry:
                sc.history.append(t_entry)

    # Parseia exceções
    in_exceptions = False
    for line in lines:
        if "### Exceções" in line:
            in_exceptions = True
            continue
        if in_exceptions:
            if line.startswith("###"):
                in_exceptions = False
                continue
            e_entry = _parse_exception_row(line)
            if e_entry:
                sc.exceptions.append(e_entry)

    # Parseia aprovações
    in_approvals = False
    for line in lines:
        if "### Aprovações" in line:
            in_approvals = True
            continue
        if in_approvals:
            if line.startswith("###"):
                in_approvals = False
                continue
            a_entry = _parse_approval_row(line)
            if a_entry:
                sc.approvals.append(a_entry)

    # Parseia resultado do reviewer
    in_reviewer = False
    rev_approved: bool | None = None
    rev_sha = ""
    rev_reviewer = "kiro-reviewer"
    rev_comments: list[str] = []
    in_comments_list = False
    for line in lines:
        if "### Resultado do Reviewer" in line:
            in_reviewer = True
            in_comments_list = False
            continue
        if in_reviewer:
            if line.startswith("###"):
                in_reviewer = False
                in_comments_list = False
                continue
            if "**Status:**" in line:
                rev_approved = "✅" in line or "aprovado" in line.lower()
                in_comments_list = False
                continue
            if "**Reviewer:**" in line:
                rev_reviewer = line.split("**Reviewer:**", 1)[1].strip()
                continue
            if "**SHA:**" in line:
                rev_sha = line.split("**SHA:**", 1)[1].strip()
                continue
            if "**Pedidos de mudança:**" in line:
                in_comments_list = True
                continue
            if in_comments_list and line.startswith("- "):
                rev_comments.append(line[2:].strip())
    if rev_approved is not None:
        sc.reviewer_result = ReviewerResult(
            approved=rev_approved,
            comments=tuple(rev_comments),
            sha=rev_sha,
            reviewer=rev_reviewer,
        )

    return sc


def _extract_field(lines: list[str], name: str) -> str | None:
    """Extrai o valor de um campo **Nome:** valor."""
    prefix = f"**{name}:**"
    for line in lines:
        if prefix in line:
            # Remove o campo de nota "*(resolvido do título)*" se existir
            value = line.split(prefix, 1)[1].strip()
            value = re.sub(r"\*\(.*?\)\*", "", value).strip()
            return value or None
    return None


def _parse_history_row(line: str) -> TransitionEntry | None:
    """Parseia uma linha da tabela de histórico.

    Formato: | quando | de → para | quem |
    """
    if not line.startswith("|") or "---" in line or "Quando" in line:
        return None
    parts = [p.strip() for p in line.split("|") if p.strip()]
    if len(parts) < 3:
        return None
    when, transition, actor = parts[0], parts[1], parts[2]
    if "→" in transition:
        from_s, to_s = [s.strip() for s in transition.split("→", 1)]
        return TransitionEntry(when=when, from_state=from_s, to_state=to_s, actor=actor)
    return None


def _parse_exception_row(line: str) -> ExceptionEntry | None:
    """Parseia uma linha da tabela de exceções.

    Formato: | `label` | justificativa | quem | quando |
    """
    if not line.startswith("|") or "---" in line or "Exceção" in line:
        return None
    parts = [p.strip() for p in line.split("|") if p.strip()]
    if len(parts) < 4:
        return None
    label = parts[0].strip("`")
    justification, actor, when = parts[1], parts[2], parts[3]
    return ExceptionEntry(label=label, justification=justification, actor=actor, when=when)


def _parse_approval_row(line: str) -> ApprovalEntry | None:
    """Parseia uma linha da tabela de aprovações.

    Formato: | `gate` | quem | quando |
    """
    if not line.startswith("|") or "---" in line or "Gate" in line:
        return None
    parts = [p.strip() for p in line.split("|") if p.strip()]
    if len(parts) < 3:
        return None
    gate = parts[0].strip("`")
    actor, when = parts[1], parts[2]
    return ApprovalEntry(gate=gate, actor=actor, when=when)


# ---------------------------------------------------------------------------
# Helpers de acesso rápido (usados pelo executor)
# ---------------------------------------------------------------------------

def extract_bypass_justification(comment_body: str | None) -> str | None:
    """Atalho: retorna a justificativa de bypass/blocked do comentário, ou None.

    Suporta tanto o namespace novo `flow:blocked` quanto o legado `crewflow:hml-bypass`
    durante a coexistência.
    """
    if not comment_body:
        return None
    sc = parse(comment_body)
    if sc is None:
        return None
    # Namespace novo primeiro, depois legado
    result = sc.get_justification("flow:blocked")
    if result is not None:
        return result
    return sc.get_justification("crewflow:hml-bypass")


def has_equivalence_test_signal(comment_body: str | None) -> bool:
    """Atalho: verifica se o state comment sinaliza teste de equivalência."""
    if not comment_body:
        return False
    sc = parse(comment_body)
    if sc is None:
        return False
    # Sinalizado por uma exceção especial ou campo no status
    return (
        sc.get_justification("equivalencia_test") is not None
        or "equivalencia_test: true" in comment_body.lower()
    )


def get_reviewer_result_from_comment(comment_body: str | None) -> ReviewerResult | None:
    """Atalho: extrai o ReviewerResult do state comment, ou None."""
    if not comment_body:
        return None
    sc = parse(comment_body)
    if sc is None:
        return None
    return sc.reviewer_result


def get_review_iterations_from_comment(comment_body: str | None) -> int:
    """Atalho: retorna o número de iterações review↔dev registradas, ou 0."""
    if not comment_body:
        return 0
    sc = parse(comment_body)
    if sc is None:
        return 0
    return sc.review_iterations


def render_pr_review_comment(
    reviewer_result: ReviewerResult,
    issue_number: int | None = None,
    issue_url: str | None = None,
) -> str:
    """Gera o corpo do comentário de review para postar no PR.

    Formato limpo, sem o bloco <!-- KIRO-FLOW-STATE --> completo.
    O marcador PR_REVIEW_COMMENT_MARKER serve de âncora para upsert.
    """
    status_str = "✅ Aprovado" if reviewer_result.approved else "⚠️ Pedidos de mudança"
    lines: list[str] = [
        PR_REVIEW_COMMENT_MARKER,
        "## 🤖 KiroCrew Review",
        "",
        f"**Resultado:** {status_str}",
        f"**Reviewer:** {reviewer_result.reviewer}",
    ]
    if reviewer_result.sha:
        lines.append(f"**SHA:** `{reviewer_result.sha}`")

    if reviewer_result.comments:
        lines += [
            "",
            "### Pedidos de mudança",
        ]
        for c in reviewer_result.comments:
            lines.append(f"- {c}")

    lines.append("")
    ref = f"issue #{issue_number}" if issue_number else "a issue"
    if issue_url:
        ref = f"[issue #{issue_number}]({issue_url})" if issue_number else f"[a issue]({issue_url})"
    lines.append(f"*Reviewer automático — {ref}*")
    lines.append(PR_REVIEW_COMMENT_CLOSE)
    return "\n".join(lines)
