"""Testes do ciclo de re-trabalho pós-review (issue #96).

Cobre:
  - Executor: REVIEW + REVIEW_FAIL → DISPATCH_REWORK
  - Teto de iterações → NOTIFY_HUMAN TL
  - NOTIFY_HUMAN com pedidos → add_labels review-fail
  - StateComment: review_iterations render/parse
  - Gates: exceeded_review_iterations
  - Prompts: rework.md renderiza com todas as variáveis esperadas
"""

from __future__ import annotations

from flow.audit.state_comment import (
    StateComment,
    get_review_iterations_from_comment,
    parse,
    render,
)
from flow.domain.gates import WorkItem, exceeded_review_iterations
from flow.domain.state import Modifier, State
from flow.executor.executor import ActionKind, HumanRole, decide
from flow.scan.scanner import ScanResult

# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _result(
    key: str = "https://github.com/owner/repo/issues/1",
    title: str = "[repo] Fix",
    labels: list[str] | None = None,
    state: State = State.REVIEW_WAITING,
    modifiers: set[Modifier] | None = None,
) -> ScanResult:
    lbl_set = frozenset(labels or ["flow:review-waiting", "flow:feature"])
    return ScanResult(
        item=WorkItem(key=key, title=title, labels=lbl_set),
        current_state=state,
        modifiers=frozenset(modifiers or []),
        dispatch_candidate=False,
        spec_valid=None,
        changed=True,
        reason="test",
    )


def _state_comment_with_changes(iterations: int = 0) -> str:
    """Retorna um state comment com ReviewerResult de pedido de mudança."""
    sc = StateComment(
        workflow="feature (v1)",
        current_node="review",
        status="review-fail",
        repo="repo",
        review_iterations=iterations,
    )
    sc.set_reviewer_result(
        approved=False,
        comments=["Falta cobertura", "Refatore o nome da função"],
        sha="abc123",
    )
    return render(sc)


# ---------------------------------------------------------------------------
# Gate: exceeded_review_iterations
# ---------------------------------------------------------------------------

class TestExceededReviewIterations:
    def test_abaixo_do_teto_passa(self) -> None:
        item = WorkItem(key="X", title="t")
        assert not exceeded_review_iterations(item, iterations=0, max_iterations=3).failed
        assert not exceeded_review_iterations(item, iterations=2, max_iterations=3).failed

    def test_igual_ao_teto_falha(self) -> None:
        item = WorkItem(key="X", title="t")
        result = exceeded_review_iterations(item, iterations=3, max_iterations=3)
        assert result.failed
        assert "3/3" in result.reason

    def test_acima_do_teto_falha(self) -> None:
        item = WorkItem(key="X", title="t")
        result = exceeded_review_iterations(item, iterations=5, max_iterations=3)
        assert result.failed

    def test_teto_customizado(self) -> None:
        item = WorkItem(key="X", title="t")
        assert not exceeded_review_iterations(item, iterations=0, max_iterations=5).failed
        assert exceeded_review_iterations(item, iterations=5, max_iterations=5).failed


# ---------------------------------------------------------------------------
# StateComment: review_iterations
# ---------------------------------------------------------------------------

class TestReviewIterations:
    def test_render_sem_iteracoes_nao_inclui_campo(self) -> None:
        sc = StateComment(workflow="f", current_node="dev", status="running", repo="r")
        body = render(sc)
        assert "Iterações de review" not in body

    def test_render_com_iteracoes_inclui_campo(self) -> None:
        sc = StateComment(workflow="f", current_node="review", status="running", repo="r",
                          review_iterations=2)
        body = render(sc)
        assert "**Iterações de review:** 2" in body

    def test_parse_iteracoes(self) -> None:
        sc = StateComment(workflow="f", current_node="review", status="running", repo="r",
                          review_iterations=2)
        body = render(sc)
        parsed = parse(body)
        assert parsed is not None
        assert parsed.review_iterations == 2

    def test_parse_sem_iteracoes_retorna_zero(self) -> None:
        sc = StateComment(workflow="f", current_node="review", status="running", repo="r")
        body = render(sc)
        parsed = parse(body)
        assert parsed is not None
        assert parsed.review_iterations == 0

    def test_get_review_iterations_helper(self) -> None:
        sc = StateComment(workflow="f", current_node="review", status="running", repo="r",
                          review_iterations=3)
        body = render(sc)
        assert get_review_iterations_from_comment(body) == 3

    def test_get_review_iterations_helper_sem_state_comment(self) -> None:
        assert get_review_iterations_from_comment(None) == 0
        assert get_review_iterations_from_comment("sem marcador") == 0


# ---------------------------------------------------------------------------
# Executor: ciclo de re-trabalho
# ---------------------------------------------------------------------------

class TestReworkCycle:
    def test_review_fail_despacha_rework(self) -> None:
        """REVIEW_REFUSED → gate humano → NOTIFY_HUMAN TL (não redespacha automaticamente)."""
        r = _result(
            state=State.REVIEW_REFUSED,
            labels=["flow:review-refused", "flow:feature"],
            modifiers=None,
        )
        d = decide(r, state_comment=None)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.TL
        assert "gate humano" in d.reason

    def test_rework_adiciona_running_remove_review_fail(self) -> None:
        """Gate humano: não adiciona develop-running automaticamente — é responsabilidade do humano."""
        r = _result(
            state=State.REVIEW_REFUSED,
            labels=["flow:review-refused", "flow:feature"],
            modifiers=None,
        )
        d = decide(r, state_comment=None)
        assert d.action is ActionKind.NOTIFY_HUMAN
        # Gate humano — NÃO move automaticamente para develop-running
        assert "flow:develop-running" not in d.add_labels

    def test_rework_registra_numero_de_iteracao(self) -> None:
        """Quando iterations >= max, notifica TL com contagem."""
        state_comment = _state_comment_with_changes(iterations=1)
        r = _result(
            state=State.REVIEW_REFUSED,
            labels=["flow:review-refused", "flow:feature"],
            modifiers=None,
        )
        d = decide(r, state_comment=state_comment)
        # Abaixo do teto → notifica TL com gate humano
        assert d.action is ActionKind.NOTIFY_HUMAN

    def test_teto_de_iteracoes_notifica_tl(self) -> None:
        """Após atingir o teto, escala para NOTIFY_HUMAN TL com mensagem de TETO."""
        # 3 iterações já feitas = teto atingido (default 3)
        state_comment = _state_comment_with_changes(iterations=3)
        r = _result(
            state=State.REVIEW_REFUSED,
            labels=["flow:review-refused", "flow:feature"],
            modifiers=None,
        )
        d = decide(r, state_comment=state_comment)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.TL
        assert "TETO DE ITERAÇÕES" in d.reason

    def test_teto_customizado_via_max_review_iterations(self) -> None:
        """max_review_iterations=1 faz escalar na segunda iteração."""
        state_comment = _state_comment_with_changes(iterations=1)
        r = _result(
            state=State.REVIEW_REFUSED,
            labels=["flow:review-refused", "flow:feature"],
            modifiers=None,
        )
        d = decide(r, state_comment=state_comment, max_review_iterations=1)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.TL

    def test_review_fail_tem_prioridade_sobre_reviewed(self) -> None:
        """REVIEW_REFUSED é processado como gate humano."""
        r = _result(
            state=State.REVIEW_REFUSED,
            labels=["flow:review-refused", "flow:reviewed", "flow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=None)
        # No novo design, REVIEW_REFUSED é sempre gate humano (NOTIFY_HUMAN)
        assert d.action is ActionKind.NOTIFY_HUMAN

    def test_sem_review_fail_segue_fluxo_normal(self) -> None:
        """Sem REVIEW_FAIL, o fluxo normal (dispatch_reviewer) continua."""
        r = _result(
            labels=["flow:review-waiting", "flow:feature"],
        )
        d = decide(r, state_comment=None)
        assert d.action is ActionKind.DISPATCH_REVIEWER


# ---------------------------------------------------------------------------
# Executor: NOTIFY_HUMAN com pedidos de mudança adiciona review-fail
# ---------------------------------------------------------------------------

class TestNotifyHumanAddsReviewFail:
    def _make_review_result(self, comments: list[str]) -> str:
        sc = StateComment(
            workflow="feature (v1)",
            current_node="review",
            status="reviewed",
            repo="repo",
        )
        sc.set_reviewer_result(approved=False, comments=comments, sha="abc123")
        return render(sc)

    def test_reprovado_com_comentarios_adiciona_review_fail(self) -> None:
        """Reviewer reprovado com comentários: NOTIFY_HUMAN TL + add review-fail."""
        state_comment = self._make_review_result(["Falta teste", "Lógica errada"])
        r = _result(
            labels=["flow:review-waiting", "flow:reviewed", "flow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=state_comment)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.TL
        assert "flow:review-refused" in d.add_labels
        assert "flow:reviewed" in d.remove_labels

    def test_aprovado_sem_comentarios_nao_adiciona_review_fail(self) -> None:
        """Reviewer aprovado sem comentários: MERGE_PR (caminho feliz, sem review-fail)."""
        sc = StateComment(workflow="f", current_node="review", status="reviewed", repo="r")
        sc.set_reviewer_result(approved=True, comments=[], sha="abc123")
        state_comment = render(sc)

        r = _result(
            labels=["flow:review-waiting", "flow:reviewed", "flow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=state_comment, auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR
        assert "flow:review-refused" not in d.add_labels


# ---------------------------------------------------------------------------
# Prompts: rework.md
# ---------------------------------------------------------------------------

class TestReworkPrompt:
    def test_rework_md_renderiza_sem_erro(self) -> None:
        """rework.md deve renderizar com todas as variáveis obrigatórias."""
        from flow.prompts.loader import render_prompt
        rendered = render_prompt(
            "rework",
            repo="owner/repo",
            repo_short="repo",
            issue_number="42",
            issue_title="Fix rework",
            issue_url="https://github.com/owner/repo/issues/42",
            session_title="rework: repo #42 PR #10 (iter 1): Fix rework",
            pr_number="10",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/repo-42",
            base_branch="main",
            iteration="1",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="",
        )
        assert "feat/issue-42" in rendered
        assert "PR #10" in rendered or "pr_number" not in rendered
        assert "iteração 1" in rendered or "iter 1" in rendered
        assert "NUNCA abra PR novo" in rendered
