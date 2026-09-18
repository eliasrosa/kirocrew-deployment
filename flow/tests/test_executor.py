"""Testes do executor — todos sem I/O, sem mock de rede."""

from __future__ import annotations

from flow.domain.gates import WorkItem
from flow.domain.state import Modifier, State
from flow.executor.executor import (
    ActionKind,
    ExecutorDecision,
    HumanRole,
    _detect_template,
    decide,
)
from flow.scan.scanner import ScanResult

# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _result(
    key: str = "VGAT-1",
    title: str = "[api-gateway2] Fix",
    labels: list[str] | None = None,
    state: State = State.TODO,
    modifiers: set[Modifier] | None = None,
    dispatch_candidate: bool = True,
) -> ScanResult:
    lbl_set = frozenset(labels or ["crewflow:todo", "crewflow:feature"])
    return ScanResult(
        item=WorkItem(key=key, title=title, labels=lbl_set),
        current_state=state,
        modifiers=frozenset(modifiers or []),
        dispatch_candidate=dispatch_candidate,
        spec_valid=None,
        changed=True,
        reason="test",
    )


# ---------------------------------------------------------------------------
# _detect_template
# ---------------------------------------------------------------------------

class TestDetectTemplate:
    def test_hotfix_tem_prioridade(self) -> None:
        labels = frozenset({"crewflow:hotfix", "crewflow:bug"})
        assert _detect_template(labels) == "hotfix"

    def test_bug(self) -> None:
        assert _detect_template(frozenset({"crewflow:bug"})) == "bug"

    def test_debt(self) -> None:
        assert _detect_template(frozenset({"crewflow:debt"})) == "debt"

    def test_default_feature(self) -> None:
        assert _detect_template(frozenset({"crewflow:feature"})) == "feature"

    def test_sem_template_e_feature(self) -> None:
        assert _detect_template(frozenset()) == "feature"


# ---------------------------------------------------------------------------
# decide() — feature
# ---------------------------------------------------------------------------

class TestDecideFeature:
    def test_todo_despacha_dev(self) -> None:
        r = _result(state=State.TODO)
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_DEV

    def test_todo_adiciona_labels_corretas(self) -> None:
        r = _result(state=State.TODO)
        d = decide(r)
        assert "crewflow:dev" in d.add_labels
        assert "crewflow:running" in d.add_labels
        assert "crewflow:todo" in d.remove_labels

    def test_review_despacha_reviewer(self) -> None:
        r = _result(state=State.REVIEW, labels=["crewflow:review", "crewflow:feature"])
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_REVIEWER

    def test_review_marca_reviewed(self) -> None:
        r = _result(state=State.REVIEW, labels=["crewflow:review", "crewflow:feature"])
        d = decide(r)
        assert "crewflow:reviewed" in d.add_labels

    def test_dev_em_andamento_skip(self) -> None:
        r = _result(state=State.DEV, labels=["crewflow:dev", "crewflow:running"])
        d = decide(r)
        assert d.action is ActionKind.SKIP

    def test_qa_notifica_qa(self) -> None:
        r = _result(state=State.QA, labels=["crewflow:qa"])
        d = decide(r)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.QA

    def test_done_skip(self) -> None:
        r = _result(state=State.DONE, labels=["crewflow:done"])
        d = decide(r)
        assert d.action is ActionKind.SKIP

    def test_sem_estado_skip(self) -> None:
        r2 = ScanResult(
            item=WorkItem(key="X", title="t", labels=frozenset({"phase-1"})),
            current_state=None,
            modifiers=frozenset(),
            dispatch_candidate=False,
            spec_valid=None,
            changed=False,
            reason="test",
        )
        d = decide(r2)
        assert d.action is ActionKind.SKIP


# ---------------------------------------------------------------------------
# decide() — lock anti-loop crewflow:reviewed
# ---------------------------------------------------------------------------

class TestAntiLoopReviewed:
    def test_review_com_reviewed_skip(self) -> None:
        """Se já foi analisado neste SHA, não dispara de novo."""
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r)
        assert d.action is ActionKind.SKIP
        assert "anti-loop" in d.reason

    def test_review_sem_reviewed_despacha(self) -> None:
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:feature"],
        )
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_REVIEWER


# ---------------------------------------------------------------------------
# decide() — hotfix
# ---------------------------------------------------------------------------

class TestDecideHotfix:
    def test_hotfix_com_p1_despacha(self) -> None:
        r = _result(
            state=State.TODO,
            labels=["crewflow:todo", "crewflow:hotfix", "crewflow:p1"],
        )
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_DEV

    def test_hotfix_sem_p1_rebaixa_para_bug(self) -> None:
        """GATE 0: hotfix sem p1 troca para template bug."""
        r = _result(
            state=State.TODO,
            labels=["crewflow:todo", "crewflow:hotfix"],
        )
        d = decide(r)
        assert d.action is ActionKind.REBRAND
        assert d.new_template == "bug"
        assert "crewflow:bug" in d.add_labels
        assert "crewflow:hotfix" in d.remove_labels

    def test_hml_bypass_sem_justificativa_bloqueia(self) -> None:
        r = _result(
            state=State.QA,
            labels=["crewflow:qa", "crewflow:hotfix", "crewflow:p1", "crewflow:hml-bypass"],
            modifiers={Modifier.HML_BYPASS},
        )
        d = decide(r, state_comment=None)
        assert d.action is ActionKind.BLOCK
        assert "justificativa" in d.block_reason.lower()

    def test_hml_bypass_com_justificativa_passa(self) -> None:
        state_comment = (
            "<!-- KIRO-FLOW-STATE -->\n"
            "### Exceções\n"
            "| Exceção | Justificativa | Quem | Quando |\n"
            "| `crewflow:hml-bypass` | Sistema de pagamento fora do ar | @elias | 2026-09-15 |\n"
        )
        r = _result(
            state=State.QA,
            labels=["crewflow:qa", "crewflow:hotfix", "crewflow:p1", "crewflow:hml-bypass"],
            modifiers={Modifier.HML_BYPASS},
        )
        d = decide(r, state_comment=state_comment)
        # Com justificativa, passa para a ação normal de QA
        assert d.action is not ActionKind.BLOCK


# ---------------------------------------------------------------------------
# decide() — débito técnico
# ---------------------------------------------------------------------------

class TestDecideDebt:
    def test_debt_todo_notifica_tl(self) -> None:
        """GATE DT: TL precisa aprovar entrada do débito técnico."""
        r = _result(
            state=State.TODO,
            labels=["crewflow:todo", "crewflow:debt"],
        )
        d = decide(r)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.TL
        assert "TL" in d.reason

    def test_debt_dev_sem_cov_notifica_dev(self) -> None:
        """Pré-condição COV: sem sinal de teste de equivalência."""
        r = _result(
            state=State.DEV,
            labels=["crewflow:dev", "crewflow:debt", "crewflow:running"],
            modifiers={Modifier.RUNNING},
        )
        d = decide(r, state_comment=None)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.DEV

    def test_debt_dev_com_cov_skip(self) -> None:
        """Com sinal de equivalência, o dev pode continuar."""
        r = _result(
            state=State.DEV,
            labels=["crewflow:dev", "crewflow:debt", "crewflow:running"],
            modifiers={Modifier.RUNNING},
        )
        state_comment = "<!-- KIRO-FLOW-STATE -->\nequivalencia_test: true\n"
        d = decide(r, state_comment=state_comment)
        # Dev + running = skip (está em andamento com COV ok)
        assert d.action is ActionKind.SKIP


# ---------------------------------------------------------------------------
# ExecutorDecision
# ---------------------------------------------------------------------------

class TestExecutorDecision:
    def test_decision_e_frozen(self) -> None:
        d = ExecutorDecision(action=ActionKind.SKIP, reason="teste")
        import pytest
        with pytest.raises((AttributeError, TypeError)):
            d.action = ActionKind.BLOCK  # type: ignore[misc]

    def test_campos_defaults(self) -> None:
        d = ExecutorDecision(action=ActionKind.SKIP, reason="ok")
        assert d.notify_role is None
        assert d.new_template is None
        assert d.block_reason == ""
        assert d.add_labels == ()
        assert d.remove_labels == ()
