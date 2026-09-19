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
    def test_review_com_reviewed_sem_resultado_skip(self) -> None:
        """crewflow:reviewed presente mas sem resultado do reviewer → SKIP (aguardando)."""
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r)
        assert d.action is ActionKind.SKIP
        assert "ainda não disponível" in d.reason

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
        from flow.audit.state_comment import StateComment, render
        sc = StateComment(workflow="hotfix (v1)", current_node="qa", status="running", repo="api-gateway2")
        sc.add_exception("crewflow:hml-bypass", "Sistema de pagamento fora do ar", "@elias", "2026-09-15")
        state_comment = render(sc)

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
        from flow.audit.state_comment import StateComment, render
        sc = StateComment(workflow="debt (v1)", current_node="dev", status="running", repo="api-gateway2")
        sc.add_exception("equivalencia_test", "teste escrito antes da refatoração", "kiro-dev", "2026-09-15")
        state_comment = render(sc)

        r = _result(
            state=State.DEV,
            labels=["crewflow:dev", "crewflow:debt", "crewflow:running"],
            modifiers={Modifier.RUNNING},
        )
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


# ---------------------------------------------------------------------------
# decide() com squad config
# ---------------------------------------------------------------------------

class TestDecideComSquadConfig:
    def _squad_com_routing(self) -> object:
        from flow.config.squad import _parse_squad
        return _parse_squad({
            "id": "test",
            "issue_provider": "github",
            "repos": ["owner/repo"],
            "routing": [
                {"match": {"labels": ["crewflow:hotfix"]}, "workflow": "hotfix-flow"},
                {"match": {"labels": ["crewflow:bug"]}, "workflow": "bug-flow"},
                {"match": {"labels": ["crewflow:debt"]}, "workflow": "debt-flow"},
                {"default": "feature-flow"},
            ],
        })

    def test_usa_resolve_workflow_do_squad(self) -> None:
        """Com squad, usa routing declarativo em vez de _detect_template."""
        squad = self._squad_com_routing()
        r = _result(state=State.TODO, labels=["crewflow:todo", "crewflow:hotfix", "crewflow:p1"])
        d = decide(r, squad=squad)
        # hotfix com p1 → dispatch_dev (não notifica TL como debt faria)
        assert d.action is ActionKind.DISPATCH_DEV

    def test_squad_none_usa_fallback(self) -> None:
        """Sem squad, continua funcionando com _detect_template."""
        r = _result(state=State.TODO, labels=["crewflow:todo", "crewflow:feature"])
        d = decide(r, squad=None)
        assert d.action is ActionKind.DISPATCH_DEV

    def test_squad_resolve_workflow_debt(self) -> None:
        """Com squad, o routing de debt vai pelo caminho correto."""
        squad = self._squad_com_routing()
        r = _result(state=State.TODO, labels=["crewflow:todo", "crewflow:debt"])
        d = decide(r, squad=squad)
        # debt + TODO → notifica TL (GATE DT)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.TL


# ---------------------------------------------------------------------------
# decide() — GATE 2: reviewer automático + merge automático
# ---------------------------------------------------------------------------

class TestGate2AutoMerge:
    """Testa os 3 caminhos do GATE 2 (reviewer + merge automático)."""

    def _make_review_result(
        self,
        approved: bool = True,
        comments: list[str] | None = None,
    ) -> str:
        """Retorna um state_comment com resultado do reviewer."""
        from flow.audit.state_comment import StateComment, render
        sc = StateComment(
            workflow="feature (v1)",
            current_node="review",
            status="reviewed",
            repo="kirocrew-flow",
        )
        sc.set_reviewer_result(
            approved=approved,
            comments=comments or [],
            sha="abc123",
        )
        return render(sc)

    def test_review_com_reviewed_sem_resultado_skip(self) -> None:
        """crewflow:reviewed presente mas sem ReviewerResult → SKIP (reviewer ainda rodando)."""
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=None)
        assert d.action is ActionKind.SKIP
        assert "ainda não disponível" in d.reason

    def test_review_com_reviewed_aprovado_sem_comentarios_merge(self) -> None:
        """Reviewer aprovado, zero comentários → MERGE_PR (caminho feliz)."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=state_comment, auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR
        assert "crewflow:done" in d.add_labels
        assert "crewflow:review" in d.remove_labels
        assert "crewflow:reviewed" in d.remove_labels

    def test_review_com_reviewed_aprovado_com_comentarios_notifica_tl(self) -> None:
        """Reviewer aprovado mas com comentários → NOTIFY_HUMAN TL."""
        state_comment = self._make_review_result(
            approved=True,
            comments=["Falta cobertura em scanner.py"],
        )
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=state_comment)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.TL
        assert "Falta cobertura" in d.reason

    def test_review_com_reviewed_reprovado_notifica_tl(self) -> None:
        """Reviewer reprovado com comentários → NOTIFY_HUMAN TL."""
        state_comment = self._make_review_result(
            approved=False,
            comments=["Lógica incorreta", "Sem testes"],
        )
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=state_comment)
        assert d.action is ActionKind.NOTIFY_HUMAN
        assert d.notify_role is HumanRole.TL

    def test_review_sem_reviewed_despacha_reviewer(self) -> None:
        """Sem crewflow:reviewed → dispara o reviewer (caminho normal)."""
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:feature"],
        )
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_REVIEWER
        assert "crewflow:reviewed" in d.add_labels

    def test_merge_pr_labels_corretas(self) -> None:
        """MERGE_PR deve adicionar done e remover review+reviewed."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=state_comment, auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR
        assert set(d.add_labels) == {"crewflow:done"}
        assert "crewflow:review" in d.remove_labels
        assert "crewflow:reviewed" in d.remove_labels

    # ── SHA verification ──────────────────────────────────────────────

    def test_sha_divergente_redespacha_reviewer(self) -> None:
        """Push pós-review: SHA do PR diverge do SHA do reviewer → redespacha."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        # SHA do PR mudou após a review (reviewer usou "abc123", PR agora em "deadbeef...")
        d = decide(r, state_comment=state_comment, pr_head_sha="deadbeef123")
        assert d.action is ActionKind.DISPATCH_REVIEWER
        assert "SHA divergiu" in d.reason
        assert "abc123" in d.reason    # SHA do reviewer (primeiros 8 chars)
        assert "deadbeef" in d.reason  # SHA do PR (primeiros 8 chars)

    def test_sha_igual_prossegue_merge(self) -> None:
        """SHA do PR bate com o do reviewer (primeiros 8 chars) → merge prossegue."""
        from flow.audit.state_comment import StateComment, render
        sc = StateComment(
            workflow="feature (v1)",
            current_node="review",
            status="reviewed",
            repo="kirocrew-flow",
        )
        sc.set_reviewer_result(approved=True, comments=[], sha="abc12345def")
        state_comment = render(sc)

        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        # Mesmo prefixo de 8 chars — SHA completo do PR pode ser maior
        d = decide(r, state_comment=state_comment, pr_head_sha="abc12345xyz", auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR

    def test_sha_ausente_no_reviewer_nao_bloqueia(self) -> None:
        """Reviewer sem SHA registrado → não bloqueia (sem info suficiente)."""
        from flow.audit.state_comment import StateComment, render
        sc = StateComment(
            workflow="feature (v1)",
            current_node="review",
            status="reviewed",
            repo="kirocrew-flow",
        )
        # sha="" — reviewer antigo sem SHA
        sc.set_reviewer_result(approved=True, comments=[], sha="")
        state_comment = render(sc)

        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=state_comment, pr_head_sha="newsha123", auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR  # sem SHA do reviewer → não bloqueia

    def test_pr_head_sha_ausente_nao_bloqueia(self) -> None:
        """Sem pr_head_sha (ex: Jira) → não bloqueia (sem info suficiente)."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )
        d = decide(r, state_comment=state_comment, pr_head_sha=None, auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR  # sem SHA do PR → não bloqueia


# ---------------------------------------------------------------------------
# decide() — auto_merge_on_approve flag (issue #91)
# ---------------------------------------------------------------------------

class TestAutoMergeOnApprove:
    """Testa o comportamento da flag auto_merge_on_approve."""

    def _make_review_result(
        self,
        approved: bool = True,
        comments: list[str] | None = None,
    ) -> str:
        from flow.audit.state_comment import StateComment, render
        sc = StateComment(
            workflow="feature (v1)",
            current_node="review",
            status="reviewed",
            repo="kirocrew-flow",
        )
        sc.set_reviewer_result(approved=approved, comments=comments or [], sha="abc123")
        return render(sc)

    def _result_review(self) -> ScanResult:
        return _result(
            state=State.REVIEW,
            labels=["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
            modifiers={Modifier.REVIEWED},
        )

    def _squad(self, auto_merge: bool) -> object:
        from flow.config.squad import _parse_squad
        return _parse_squad({
            "id": "test",
            "issue_provider": "github",
            "repos": ["owner/repo"],
            "workflow_params": {"auto_merge_on_approve": auto_merge},
        })

    # ── flag explícita no parâmetro ────────────────────────────────────────

    def test_explicito_true_emite_merge_pr(self) -> None:
        """auto_merge_on_approve=True explícito → MERGE_PR."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = self._result_review()
        d = decide(r, state_comment=state_comment, auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR

    def test_explicito_false_emite_skip(self) -> None:
        """auto_merge_on_approve=False explícito → SKIP (merge manual)."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = self._result_review()
        d = decide(r, state_comment=state_comment, auto_merge_on_approve=False)
        assert d.action is ActionKind.SKIP
        assert "merge manual" in d.reason
        assert "auto_merge_on_approve=false" in d.reason

    # ── flag lida da squad config ──────────────────────────────────────────

    def test_squad_auto_merge_true_emite_merge_pr(self) -> None:
        """squad.workflow_params.auto_merge_on_approve=True → MERGE_PR."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = self._result_review()
        squad = self._squad(auto_merge=True)
        d = decide(r, state_comment=state_comment, squad=squad)
        assert d.action is ActionKind.MERGE_PR

    def test_squad_auto_merge_false_emite_skip(self) -> None:
        """squad.workflow_params.auto_merge_on_approve=False → SKIP (merge manual)."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = self._result_review()
        squad = self._squad(auto_merge=False)
        d = decide(r, state_comment=state_comment, squad=squad)
        assert d.action is ActionKind.SKIP

    def test_default_sem_squad_e_false(self) -> None:
        """Sem squad e sem parâmetro → default False → SKIP."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = self._result_review()
        d = decide(r, state_comment=state_comment)
        assert d.action is ActionKind.SKIP

    # ── flag explícita tem prioridade sobre squad config ──────────────────

    def test_parametro_explicito_sobrescreve_squad(self) -> None:
        """auto_merge_on_approve=True explícito sobrescreve squad=False."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = self._result_review()
        squad = self._squad(auto_merge=False)
        d = decide(r, state_comment=state_comment, squad=squad, auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR

    def test_parametro_false_sobrescreve_squad_true(self) -> None:
        """auto_merge_on_approve=False explícito sobrescreve squad=True."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = self._result_review()
        squad = self._squad(auto_merge=True)
        d = decide(r, state_comment=state_comment, squad=squad, auto_merge_on_approve=False)
        assert d.action is ActionKind.SKIP

    # ── comportamento não afeta fluxos com comentários ────────────────────

    def test_com_comentarios_notifica_tl_independente_da_flag(self) -> None:
        """Reviewer com comentários → NOTIFY_HUMAN TL independente da flag."""
        state_comment = self._make_review_result(approved=True, comments=["Falta cobertura"])
        r = self._result_review()
        # Com flag on — ainda notifica TL (não merge)
        d_on = decide(r, state_comment=state_comment, auto_merge_on_approve=True)
        assert d_on.action is ActionKind.NOTIFY_HUMAN
        assert d_on.notify_role is HumanRole.TL
        # Com flag off — mesmo comportamento
        d_off = decide(r, state_comment=state_comment, auto_merge_on_approve=False)
        assert d_off.action is ActionKind.NOTIFY_HUMAN

    # ── merge_pr adiciona labels corretas quando flag on ──────────────────

    def test_merge_pr_labels_com_flag_on(self) -> None:
        """Com flag on, MERGE_PR tem as labels corretas."""
        state_comment = self._make_review_result(approved=True, comments=[])
        r = self._result_review()
        d = decide(r, state_comment=state_comment, auto_merge_on_approve=True)
        assert d.action is ActionKind.MERGE_PR
        assert "crewflow:done" in d.add_labels
        assert "crewflow:review" in d.remove_labels
        assert "crewflow:reviewed" in d.remove_labels
