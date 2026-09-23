"""Testes do comentário de estado — sem I/O, sem mock."""

from __future__ import annotations

from flow.audit.state_comment import (
    MARKER_CLOSE,
    MARKER_OPEN,
    StateComment,
    extract_bypass_justification,
    has_equivalence_test_signal,
    parse,
    render,
)

# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _make_sc(
    workflow: str = "feature (v1)",
    node: str = "dev",
    status: str = "running",
    repo: str = "api-gateway2",
) -> StateComment:
    return StateComment(workflow=workflow, current_node=node, status=status, repo=repo)


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------

class TestRender:
    def test_tem_marcadores(self) -> None:
        sc = _make_sc()
        text = render(sc)
        assert MARKER_OPEN in text
        assert MARKER_CLOSE in text

    def test_campos_presentes(self) -> None:
        sc = _make_sc(workflow="hotfix (v1)", node="review", status="waiting")
        text = render(sc)
        assert "**Workflow:** hotfix (v1)" in text
        assert "**Nó atual:** review" in text
        assert "**Status:** waiting" in text
        assert "**Repo:** api-gateway2" in text

    def test_historico_renderizado(self) -> None:
        sc = _make_sc()
        sc.add_transition("start", "flow:develop-running", "system", "2026-09-15 00:02")
        text = render(sc)
        assert "### Histórico" in text
        assert "start → flow:develop-running" in text
        assert "system" in text

    def test_excecoes_renderizadas(self) -> None:
        sc = _make_sc()
        sc.add_exception("flow:blocked", "Checkout fora do ar", "@elias", "2026-09-15")
        text = render(sc)
        assert "### Exceções" in text
        assert "flow:blocked" in text
        assert "Checkout fora do ar" in text

    def test_sem_historico_nao_tem_secao(self) -> None:
        sc = _make_sc()
        text = render(sc)
        assert "### Histórico" not in text

    def test_sem_excecoes_nao_tem_secao(self) -> None:
        sc = _make_sc()
        text = render(sc)
        assert "### Exceções" not in text

    def test_marcadores_abrem_e_fecham(self) -> None:
        sc = _make_sc()
        text = render(sc)
        open_pos = text.index(MARKER_OPEN)
        close_pos = text.index(MARKER_CLOSE)
        assert open_pos < close_pos


# ---------------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------------

class TestParse:
    def test_retorna_none_sem_marcador(self) -> None:
        assert parse("comentário normal sem marcador") is None

    def test_retorna_none_string_vazia(self) -> None:
        assert parse("") is None

    def test_roundtrip_basico(self) -> None:
        sc = _make_sc(workflow="feature (v1)", node="review", status="waiting")
        text = render(sc)
        parsed = parse(text)
        assert parsed is not None
        assert parsed.workflow == "feature (v1)"
        assert parsed.current_node == "review"
        assert parsed.status == "waiting"
        assert parsed.repo == "api-gateway2"

    def test_roundtrip_com_historico(self) -> None:
        sc = _make_sc()
        sc.add_transition("start", "flow:develop-running", "system", "2026-09-15 00:02")
        sc.add_transition("flow:develop-running", "flow:review-waiting", "kiro-dev", "2026-09-15 00:08")
        text = render(sc)
        parsed = parse(text)
        assert parsed is not None
        assert len(parsed.history) == 2
        assert parsed.history[0].from_state == "start"
        assert parsed.history[0].to_state == "flow:develop-running"
        assert parsed.history[0].actor == "system"
        assert parsed.history[1].from_state == "flow:develop-running"

    def test_roundtrip_com_excecao(self) -> None:
        sc = _make_sc()
        sc.add_exception("flow:blocked", "Checkout fora do ar", "@elias", "2026-09-15")
        text = render(sc)
        parsed = parse(text)
        assert parsed is not None
        assert len(parsed.exceptions) == 1
        exc = parsed.exceptions[0]
        assert exc.label == "flow:blocked"
        assert exc.justification == "Checkout fora do ar"
        assert exc.actor == "@elias"

    def test_parse_no_meio_de_comentario_maior(self) -> None:
        """O marcador pode estar no meio de um comentário com mais texto."""
        prefix = "Aqui vai algum texto antes do marcador.\n\n"
        suffix = "\n\nTexto depois do marcador."
        sc = _make_sc()
        body = prefix + render(sc) + suffix
        parsed = parse(body)
        assert parsed is not None
        assert parsed.workflow == "feature (v1)"

    def test_parse_ignora_tabelas_de_cabecalho(self) -> None:
        sc = _make_sc()
        sc.add_transition("start", "dev", "system", "2026-09-15")
        text = render(sc)
        parsed = parse(text)
        # O header "|-----..." não deve virar TransitionEntry
        assert parsed is not None
        assert all(e.from_state != "---" for e in parsed.history)


# ---------------------------------------------------------------------------
# StateComment — operações
# ---------------------------------------------------------------------------

class TestStateComment:
    def test_get_justification_presente(self) -> None:
        sc = _make_sc()
        sc.add_exception("flow:blocked", "Perda de receita", "@elias", "2026-09-15")
        assert sc.get_justification("flow:blocked") == "Perda de receita"

    def test_get_justification_ausente(self) -> None:
        sc = _make_sc()
        assert sc.get_justification("flow:blocked") is None

    def test_add_transition_usa_timestamp_automatico(self) -> None:
        sc = _make_sc()
        sc.add_transition("start", "dev", "system")
        assert len(sc.history) == 1
        assert sc.history[0].when != ""  # timestamp foi gerado

    def test_multiplas_transicoes_em_ordem(self) -> None:
        sc = _make_sc()
        sc.add_transition("start", "dev", "system", "2026-09-15 00:01")
        sc.add_transition("dev", "review", "kiro-dev", "2026-09-15 00:05")
        assert sc.history[0].to_state == "dev"
        assert sc.history[1].to_state == "review"


# ---------------------------------------------------------------------------
# Helpers de acesso rápido
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_extract_bypass_retorna_none_sem_comentario(self) -> None:
        assert extract_bypass_justification(None) is None
        assert extract_bypass_justification("sem marcador") is None

    def test_extract_bypass_encontra_justificativa(self) -> None:
        sc = _make_sc()
        sc.add_exception("flow:blocked", "Sistema fora do ar", "@elias", "2026-09-15")
        text = render(sc)
        j = extract_bypass_justification(text)
        assert j == "Sistema fora do ar"

    def test_extract_bypass_retorna_none_sem_excecao(self) -> None:
        sc = _make_sc()
        text = render(sc)
        assert extract_bypass_justification(text) is None

    def test_has_equivalence_test_sem_sinal(self) -> None:
        sc = _make_sc()
        text = render(sc)
        assert has_equivalence_test_signal(text) is False

    def test_has_equivalence_test_com_flag_inline(self) -> None:
        sc = _make_sc()
        text = render(sc) + "\nequivalencia_test: true"
        assert has_equivalence_test_signal(text) is True

    def test_has_equivalence_test_com_excecao(self) -> None:
        sc = _make_sc()
        sc.add_exception("equivalencia_test", "teste escrito antes da refatoração", "kiro-dev", "2026-09-15")
        text = render(sc)
        assert has_equivalence_test_signal(text) is True


# ---------------------------------------------------------------------------
# ReviewerResult
# ---------------------------------------------------------------------------

class TestReviewerResult:
    def test_is_auto_mergeable_sem_comentarios(self) -> None:
        from flow.audit.state_comment import ReviewerResult
        rr = ReviewerResult(approved=True, comments=())
        assert rr.is_auto_mergeable is True

    def test_nao_auto_mergeable_com_comentarios(self) -> None:
        from flow.audit.state_comment import ReviewerResult
        rr = ReviewerResult(approved=True, comments=("Falta teste",))
        assert rr.is_auto_mergeable is False

    def test_nao_auto_mergeable_reprovado(self) -> None:
        from flow.audit.state_comment import ReviewerResult
        rr = ReviewerResult(approved=False, comments=())
        assert rr.is_auto_mergeable is False

    def test_set_reviewer_result_persiste(self) -> None:
        sc = _make_sc()
        sc.set_reviewer_result(approved=True, comments=[], sha="abc123", reviewer="kiro-reviewer")
        rr = sc.get_reviewer_result()
        assert rr is not None
        assert rr.approved is True
        assert rr.sha == "abc123"
        assert rr.is_auto_mergeable is True

    def test_roundtrip_reviewer_result_aprovado(self) -> None:
        sc = _make_sc()
        sc.set_reviewer_result(approved=True, comments=[], sha="deadbeef")
        text = render(sc)
        parsed = parse(text)
        assert parsed is not None
        rr = parsed.reviewer_result
        assert rr is not None
        assert rr.approved is True
        assert rr.is_auto_mergeable is True

    def test_roundtrip_reviewer_result_com_comentarios(self) -> None:
        sc = _make_sc()
        sc.set_reviewer_result(
            approved=False,
            comments=["Falta cobertura em X", "Nome de variável confuso"],
            sha="deadbeef",
        )
        text = render(sc)
        parsed = parse(text)
        assert parsed is not None
        rr = parsed.reviewer_result
        assert rr is not None
        assert rr.approved is False
        assert len(rr.comments) == 2
        assert rr.comments[0] == "Falta cobertura em X"
        assert rr.is_auto_mergeable is False

    def test_roundtrip_sem_reviewer_result(self) -> None:
        sc = _make_sc()
        text = render(sc)
        parsed = parse(text)
        assert parsed is not None
        assert parsed.reviewer_result is None

    def test_get_reviewer_result_from_comment_aprovado(self) -> None:
        from flow.audit.state_comment import get_reviewer_result_from_comment
        sc = _make_sc()
        sc.set_reviewer_result(approved=True, comments=[])
        text = render(sc)
        rr = get_reviewer_result_from_comment(text)
        assert rr is not None
        assert rr.is_auto_mergeable is True

    def test_get_reviewer_result_from_comment_none(self) -> None:
        from flow.audit.state_comment import get_reviewer_result_from_comment
        assert get_reviewer_result_from_comment(None) is None
        assert get_reviewer_result_from_comment("sem marcador") is None


# ---------------------------------------------------------------------------
# render_pr_review_comment
# ---------------------------------------------------------------------------

class TestRenderPrReviewComment:
    def test_aprovado_sem_comentarios(self) -> None:
        from flow.audit.state_comment import ReviewerResult, render_pr_review_comment
        rr = ReviewerResult(approved=True, comments=(), sha="", reviewer="kiro-reviewer")
        body = render_pr_review_comment(rr, issue_number=73)
        assert "## 🤖 KiroCrew Review" in body
        assert "**Resultado:** ✅ Aprovado" in body
        assert "issue #73" in body
        # sem pedidos de mudança quando aprovado sem comentários
        assert "Pedidos de mudança" not in body

    def test_pedidos_de_mudanca_com_comentarios(self) -> None:
        from flow.audit.state_comment import ReviewerResult, render_pr_review_comment
        comments = [
            "deployment.py: import local sem justificativa",
            "_dry_run_report(): skipped pode ser negativo",
        ]
        rr = ReviewerResult(approved=False, comments=tuple(comments), sha="", reviewer="kiro-reviewer")
        body = render_pr_review_comment(rr, issue_number=73)
        assert "**Resultado:** ⚠️ Pedidos de mudança" in body
        assert "Pedidos de mudança" in body
        assert "- deployment.py: import local sem justificativa" in body
        assert "- _dry_run_report(): skipped pode ser negativo" in body

    def test_um_bullet_por_comentario(self) -> None:
        from flow.audit.state_comment import ReviewerResult, render_pr_review_comment
        comments = ["a", "b", "c"]
        rr = ReviewerResult(approved=False, comments=tuple(comments), sha="", reviewer="kiro-reviewer")
        body = render_pr_review_comment(rr, issue_number=1
        )
        assert body.count("\n- ") == 3


# ---------------------------------------------------------------------------
# render_issue_pr_reference
# ---------------------------------------------------------------------------

class TestRenderIssuePrReference:
    def test_aprovado(self) -> None:
        from flow.audit.state_comment import render_issue_pr_reference
        ref = render_issue_pr_reference(pr_number=74, approved=True)
        assert "PR #74" in ref
        assert "aprovado" in ref
        assert "pedidos de mudança" not in ref

    def test_pedidos_de_mudanca(self) -> None:
        from flow.audit.state_comment import render_issue_pr_reference
        ref = render_issue_pr_reference(pr_number=74, approved=False)
        assert "PR #74" in ref
        assert "pedidos de mudança" in ref

    def test_nao_duplica_detalhe(self) -> None:
        """A referência curta não deve conter bullets de detalhe."""
        from flow.audit.state_comment import render_issue_pr_reference
        ref = render_issue_pr_reference(pr_number=74, approved=False)
        assert "### Pedidos de mudança" not in ref
        assert "\n- " not in ref
