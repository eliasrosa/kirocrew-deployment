"""Testes para implicit_state() — shadow mode (issue #178).

Puro Python, sem I/O.  A função recebe os dados como parâmetros.
"""

from __future__ import annotations

from flow.scan.scanner import ImplicitState, implicit_state

# ---------------------------------------------------------------------------
# Casos básicos
# ---------------------------------------------------------------------------


class TestImplicitStateBasic:
    def test_sem_branch_sem_pr_retorna_todo(self) -> None:
        result = implicit_state(
            issue_closed=False,
            branches=[],
            prs=[],
            issue_number=42,
        )
        assert result is ImplicitState.TODO

    def test_issue_fechada_retorna_done(self) -> None:
        result = implicit_state(
            issue_closed=True,
            branches=["feat/issue-42"],
            prs=[{"state": "open", "reviews": []}],
            issue_number=42,
        )
        assert result is ImplicitState.DONE

    def test_issue_fechada_sem_evidencias_retorna_done(self) -> None:
        result = implicit_state(
            issue_closed=True,
            branches=[],
            prs=[],
            issue_number=42,
        )
        assert result is ImplicitState.DONE

    def test_branch_sem_pr_retorna_dev(self) -> None:
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-42"],
            prs=[],
            issue_number=42,
        )
        assert result is ImplicitState.DEV

    def test_pr_aberta_sem_aprovacao_retorna_review(self) -> None:
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-42"],
            prs=[{"state": "open", "reviews": []}],
            issue_number=42,
        )
        assert result is ImplicitState.REVIEW

    def test_pr_aberta_com_aprovacao_retorna_review_ok(self) -> None:
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-42"],
            prs=[{"state": "open", "reviews": [{"state": "APPROVED"}]}],
            issue_number=42,
        )
        assert result is ImplicitState.REVIEW_OK

    def test_pr_aberta_com_changes_requested_retorna_review(self) -> None:
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-42"],
            prs=[{"state": "open", "reviews": [{"state": "CHANGES_REQUESTED"}]}],
            issue_number=42,
        )
        assert result is ImplicitState.REVIEW


# ---------------------------------------------------------------------------
# Prioridade: done > review_ok > review > dev > todo
# ---------------------------------------------------------------------------


class TestImplicitStatePrioridade:
    def test_done_tem_prioridade_sobre_pr_aberta(self) -> None:
        """Issue fechada + PR aberta → DONE (PR pode estar aberta mas issue foi fechada)."""
        result = implicit_state(
            issue_closed=True,
            branches=["feat/issue-10"],
            prs=[{"state": "open", "reviews": [{"state": "APPROVED"}]}],
            issue_number=10,
        )
        assert result is ImplicitState.DONE

    def test_review_ok_tem_prioridade_sobre_review(self) -> None:
        """PR aberta com aprovação → REVIEW_OK, não REVIEW."""
        result = implicit_state(
            issue_closed=False,
            branches=[],
            prs=[{"state": "open", "reviews": [{"state": "APPROVED"}]}],
            issue_number=5,
        )
        assert result is ImplicitState.REVIEW_OK

    def test_review_tem_prioridade_sobre_dev(self) -> None:
        """PR aberta sem aprovação → REVIEW, mesmo com branch existente."""
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-5"],
            prs=[{"state": "open", "reviews": []}],
            issue_number=5,
        )
        assert result is ImplicitState.REVIEW

    def test_dev_tem_prioridade_sobre_todo(self) -> None:
        """Branch existente sem PR → DEV, não TODO."""
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-5"],
            prs=[],
            issue_number=5,
        )
        assert result is ImplicitState.DEV


# ---------------------------------------------------------------------------
# Filtragem de PRs por state=open
# ---------------------------------------------------------------------------


class TestImplicitStatePRFilter:
    def test_pr_fechada_e_ignorada(self) -> None:
        """PR com state=closed não conta como evidência de REVIEW."""
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-7"],
            prs=[{"state": "closed", "reviews": [{"state": "APPROVED"}]}],
            issue_number=7,
        )
        # Branch existe mas PR está fechada → DEV
        assert result is ImplicitState.DEV

    def test_pr_merged_e_ignorada(self) -> None:
        """PR com state=merged não conta como evidência de REVIEW."""
        result = implicit_state(
            issue_closed=False,
            branches=[],
            prs=[{"state": "merged", "reviews": [{"state": "APPROVED"}]}],
            issue_number=7,
        )
        assert result is ImplicitState.TODO

    def test_multiplas_prs_uma_aberta_com_aprovacao(self) -> None:
        """Apenas PRs abertas contam; se alguma tiver aprovação, REVIEW_OK."""
        result = implicit_state(
            issue_closed=False,
            branches=[],
            prs=[
                {"state": "closed", "reviews": [{"state": "APPROVED"}]},
                {"state": "open", "reviews": [{"state": "APPROVED"}]},
            ],
            issue_number=8,
        )
        assert result is ImplicitState.REVIEW_OK

    def test_reviews_ausentes_nao_causa_erro(self) -> None:
        """PR sem campo 'reviews' não deve lançar exceção."""
        result = implicit_state(
            issue_closed=False,
            branches=[],
            prs=[{"state": "open"}],
            issue_number=9,
        )
        assert result is ImplicitState.REVIEW

    def test_reviews_none_nao_causa_erro(self) -> None:
        """PR com reviews=None não deve lançar exceção."""
        result = implicit_state(
            issue_closed=False,
            branches=[],
            prs=[{"state": "open", "reviews": None}],
            issue_number=9,
        )
        assert result is ImplicitState.REVIEW


# ---------------------------------------------------------------------------
# Filtragem de branch por issue_number
# ---------------------------------------------------------------------------


class TestImplicitStateBranchFilter:
    def test_branch_canonica_correta(self) -> None:
        """Apenas feat/issue-N conta quando issue_number é fornecido."""
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-42"],
            prs=[],
            issue_number=42,
        )
        assert result is ImplicitState.DEV

    def test_branch_de_outra_issue_e_ignorada(self) -> None:
        """feat/issue-99 não conta para issue 42."""
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-99"],
            prs=[],
            issue_number=42,
        )
        assert result is ImplicitState.TODO

    def test_sem_issue_number_qualquer_branch_conta(self) -> None:
        """Sem issue_number, qualquer branch na lista conta."""
        result = implicit_state(
            issue_closed=False,
            branches=["feat/issue-99"],
            prs=[],
            issue_number=None,
        )
        assert result is ImplicitState.DEV

    def test_lista_vazia_sem_issue_number_retorna_todo(self) -> None:
        result = implicit_state(
            issue_closed=False,
            branches=[],
            prs=[],
            issue_number=None,
        )
        assert result is ImplicitState.TODO


# ---------------------------------------------------------------------------
# Valores de ImplicitState
# ---------------------------------------------------------------------------


class TestImplicitStateValues:
    def test_valores_string_corretos(self) -> None:
        assert ImplicitState.TODO.value == "todo"
        assert ImplicitState.DEV.value == "dev"
        assert ImplicitState.REVIEW.value == "review"
        assert ImplicitState.REVIEW_OK.value == "review_ok"
        assert ImplicitState.DONE.value == "done"

    def test_e_str_enum(self) -> None:
        assert str(ImplicitState.TODO) == "todo"
        assert str(ImplicitState.REVIEW_OK) == "review_ok"
