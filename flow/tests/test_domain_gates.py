"""Testes de flow/domain/gates.py.

Todos sem mock — os valores são injetados diretamente nas funções.
Os casos vieram do desenho dos 4 fluxos e das decisões de 14/09.
"""

import pytest

from flow.domain.gates import (
    GateVerdict,
    Result,
    Squad,
    TemplateSwitch,
    WorkItem,
    can_leave_spec,
    can_start_debt,
    has_equivalence_test,
    parse_repos_from_title,
    triage_hotfix,
    validate_hml_bypass,
)


# ---------------------------------------------------------------------------
# Fixtures comuns
# ---------------------------------------------------------------------------

SQUAD = Squad(
    id="cogna-gateway",
    repos=frozenset({"api-gateway2", "api-subscription2", "api-back"}),
)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

class TestResult:
    def test_success_e_truthy(self) -> None:
        assert Result.success()

    def test_fail_e_falsy(self) -> None:
        assert not Result.fail("motivo")

    def test_failed_property(self) -> None:
        assert Result.fail("x").failed
        assert not Result.success().failed

    def test_success_carrega_data(self) -> None:
        r = Result.success(data="api-gateway2")
        assert r.data == "api-gateway2"

    def test_fail_preserva_reason(self) -> None:
        r = Result.fail("título sem [repo]")
        assert "título sem [repo]" in r.reason


# ---------------------------------------------------------------------------
# parse_repos_from_title
# ---------------------------------------------------------------------------

class TestParseReposFromTitle:
    def test_formato_simples(self) -> None:
        result = parse_repos_from_title("[api-gateway2] Fix normal", SQUAD.repos)
        assert result == ["api-gateway2"]

    def test_formato_spike(self) -> None:
        """[spike][repo] — spike é prefixo ignorado."""
        result = parse_repos_from_title(
            "[spike][api-gateway2] Investigar", SQUAD.repos
        )
        assert result == ["api-gateway2"]

    def test_formato_aiden(self) -> None:
        """[voomp][repo][env] — voomp e env são prefixos ignorados."""
        result = parse_repos_from_title(
            "[voomp][api-gateway2][str] Performance", SQUAD.repos
        )
        assert result == ["api-gateway2"]

    def test_dois_repos(self) -> None:
        result = parse_repos_from_title(
            "[api-gateway2][api-subscription2] Ajuste cross-repo",
            SQUAD.repos,
        )
        assert set(result) == {"api-gateway2", "api-subscription2"}

    def test_typo_retorna_lista_vazia(self) -> None:
        """Typo falha alto: o motor recusa e avisa, não dispara no lugar errado."""
        result = parse_repos_from_title("[api-gatewy2] Fix com typo", SQUAD.repos)
        assert result == []

    def test_sem_colchetes_retorna_lista_vazia(self) -> None:
        result = parse_repos_from_title("Fix sem prefixo de repo", SQUAD.repos)
        assert result == []

    def test_repo_desconhecido_e_ignorado(self) -> None:
        result = parse_repos_from_title("[repo-inexistente] Algo", SQUAD.repos)
        assert result == []

    def test_case_insensitive(self) -> None:
        result = parse_repos_from_title("[API-GATEWAY2] Fix", SQUAD.repos)
        assert result == ["api-gateway2"]


# ---------------------------------------------------------------------------
# can_leave_spec
# ---------------------------------------------------------------------------

class TestCanLeaveSpec:
    def test_titulo_com_repo_valido_passa(self) -> None:
        item = WorkItem(key="VGAT-123", title="[api-gateway2] Ajustar validação")
        result = can_leave_spec(item, SQUAD)
        assert result
        assert result.data == "api-gateway2"

    def test_titulo_sem_repo_falha(self) -> None:
        item = WorkItem(key="VGAT-123", title="Ajustar validação sem repo")
        result = can_leave_spec(item, SQUAD)
        assert result.failed
        assert "repo" in result.reason.lower()

    def test_typo_no_repo_falha_com_mensagem_clara(self) -> None:
        item = WorkItem(key="VGAT-123", title="[api-gatewy2] Fix com typo")
        result = can_leave_spec(item, SQUAD)
        assert result.failed
        assert "api-gateway2" in result.reason  # lista os repos conhecidos

    def test_dois_repos_numa_issue_sao_recusados(self) -> None:
        """A garantia central: decomponha, não só mencione."""
        item = WorkItem(
            key="VGAT-123",
            title="[api-gateway2][api-subscription2] Ajustar fluxo",
        )
        result = can_leave_spec(item, SQUAD)
        assert result.failed
        assert "decomponha" in result.reason

    def test_subtarefa_herda_do_pai_sem_validar_titulo(self) -> None:
        """Subtarefa e Sub-bug são etapas de trabalho, não unidades independentes."""
        item = WorkItem(
            key="VGAT-124",
            title="Implementação do fix",  # sem [repo]
            parent_key="VGAT-123",
        )
        result = can_leave_spec(item, SQUAD)
        assert result
        assert result.data == "inherited_from_parent"

    def test_formato_spike_passa(self) -> None:
        item = WorkItem(
            key="VGAT-125",
            title="[spike][api-gateway2] Investigar alternativas",
        )
        result = can_leave_spec(item, SQUAD)
        assert result
        assert result.data == "api-gateway2"


# ---------------------------------------------------------------------------
# triage_hotfix — GATE 0
# ---------------------------------------------------------------------------

class TestTriageHotfix:
    def test_hotfix_com_p1_passa(self) -> None:
        item = WorkItem(
            key="VGAT-200",
            title="[api-gateway2] Fix urgente",
            labels=frozenset({"crewflow:hotfix", "crewflow:p1"}),
        )
        verdict = triage_hotfix(item)
        assert verdict.result
        assert verdict.switch is None

    def test_hotfix_sem_p1_rebaixa_para_bug(self) -> None:
        """Todo mundo acha que seu bug é hotfix. O GATE 0 filtra."""
        item = WorkItem(
            key="VGAT-201",
            title="[api-gateway2] Ajuste menor",
            labels=frozenset({"crewflow:hotfix"}),
        )
        verdict = triage_hotfix(item)
        assert verdict.result.failed
        assert verdict.switch is TemplateSwitch.BUG

    def test_verdict_e_gateVerdict(self) -> None:
        item = WorkItem(key="X", title="[api-gateway2] t", labels=frozenset({"crewflow:p1"}))
        assert isinstance(triage_hotfix(item), GateVerdict)


# ---------------------------------------------------------------------------
# can_start_debt — GATE de entrada do DT
# ---------------------------------------------------------------------------

class TestCanStartDebt:
    def test_com_aprovacao_tl_passa(self) -> None:
        item = WorkItem(key="VGAT-300", title="[api-gateway2] Refatorar cache")
        result = can_start_debt(item, has_tl_approval=True)
        assert result

    def test_sem_aprovacao_tl_falha(self) -> None:
        item = WorkItem(key="VGAT-300", title="[api-gateway2] Refatorar cache")
        result = can_start_debt(item, has_tl_approval=False)
        assert result.failed
        assert "TL" in result.reason

    def test_mensagem_explica_autoridade(self) -> None:
        item = WorkItem(key="X", title="t")
        result = can_start_debt(item, has_tl_approval=False)
        assert "técnica" in result.reason.lower()


# ---------------------------------------------------------------------------
# has_equivalence_test — pré-condição COV
# ---------------------------------------------------------------------------

class TestHasEquivalenceTest:
    def test_com_teste_passa(self) -> None:
        item = WorkItem(key="VGAT-400", title="[api-gateway2] Refatorar parser")
        result = has_equivalence_test(item, test_exists=True)
        assert result

    def test_sem_teste_falha_e_orienta(self) -> None:
        item = WorkItem(key="VGAT-400", title="[api-gateway2] Refatorar parser")
        result = has_equivalence_test(item, test_exists=False)
        assert result.failed
        assert "CÓDIGO ANTIGO" in result.reason  # orienta o dev

    def test_nao_e_gate_de_aprovacao_humana(self) -> None:
        """Pré-condição é mecânica, não humana — a função recebe um bool, não um ator."""
        item = WorkItem(key="X", title="t")
        r1 = has_equivalence_test(item, test_exists=True)
        r2 = has_equivalence_test(item, test_exists=False)
        assert r1
        assert r2.failed


# ---------------------------------------------------------------------------
# validate_hml_bypass
# ---------------------------------------------------------------------------

class TestValidateHmlBypass:
    def test_sem_bypass_passa_sempre(self) -> None:
        item = WorkItem(
            key="VGAT-500",
            title="[api-gateway2] Fix",
            labels=frozenset({"crewflow:hotfix", "crewflow:p1"}),
        )
        result = validate_hml_bypass(item, justification=None)
        assert result

    def test_bypass_com_justificativa_passa(self) -> None:
        item = WorkItem(
            key="VGAT-500",
            title="[api-gateway2] Fix urgente",
            labels=frozenset({"crewflow:hotfix", "crewflow:hml-bypass"}),
        )
        result = validate_hml_bypass(item, justification="Checkout fora do ar, perda de receita")
        assert result
        assert "Checkout" in str(result.data)

    def test_bypass_sem_justificativa_bloqueia_merge(self) -> None:
        item = WorkItem(
            key="VGAT-500",
            title="[api-gateway2] Fix urgente",
            labels=frozenset({"crewflow:hotfix", "crewflow:hml-bypass"}),
        )
        result = validate_hml_bypass(item, justification=None)
        assert result.failed
        assert "justificativa" in result.reason.lower()

    def test_bypass_com_justificativa_vazia_bloqueia(self) -> None:
        item = WorkItem(
            key="VGAT-501",
            title="[api-gateway2] Fix",
            labels=frozenset({"crewflow:hml-bypass"}),
        )
        result = validate_hml_bypass(item, justification="   ")  # só espaços
        assert result.failed

    def test_justificativa_preservada_no_data(self) -> None:
        item = WorkItem(
            key="VGAT-502",
            title="[api-gateway2] Fix",
            labels=frozenset({"crewflow:hml-bypass"}),
        )
        motivo = "Sistema de pagamento fora do ar"
        result = validate_hml_bypass(item, justification=motivo)
        assert result
        assert result.data == motivo
