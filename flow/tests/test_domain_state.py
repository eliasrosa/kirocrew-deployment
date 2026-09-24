"""Testes de flow/domain/state.py.

Todos sem mock — é exatamente o ponto da arquitetura hexagonal.
Os casos vieram do desenho dos 4 fluxos (feature, bug, hotfix, débito).
"""

import pytest

from flow.domain.state import (
    DISPATCH_TRIGGER,
    STOP_MODIFIERS,
    EstadoAmbiguo,
    Modifier,
    State,
    can_transition,
    is_dispatchable,
    parse_modifiers,
    parse_state,
    transition_state,
)

# ---------------------------------------------------------------------------
# Constantes e enum
# ---------------------------------------------------------------------------

class TestStateConstants:
    def test_gatilho_e_todo(self) -> None:
        assert DISPATCH_TRIGGER is State.DEVELOP_WAITING

    def test_stop_modifiers_contem_blocked(self) -> None:
        assert Modifier.BLOCKED in STOP_MODIFIERS

    def test_merge_conflict_nao_e_stop_modifier(self) -> None:
        """merge-conflict aciona cron de conflito mas não impede dispatch em si."""
        assert Modifier.MERGE_CONFLICT not in STOP_MODIFIERS

    def test_hml_bypass_nao_e_stop_modifier(self) -> None:
        """blocked é stop modifier — mas é verificado diretamente."""
        assert Modifier.BLOCKED in STOP_MODIFIERS

    def test_reviewed_nao_e_stop_modifier(self) -> None:
        """reviewed é lock anti-loop, não impede dispatch da issue."""
        assert Modifier.REVIEWED not in STOP_MODIFIERS

    def test_review_ok_nao_e_stop_modifier(self) -> None:
        """review-ok é resultado de review, não impede dispatch de TODO."""
        assert Modifier.REVIEWED not in STOP_MODIFIERS

    def test_review_fail_nao_e_stop_modifier(self) -> None:
        """review-fail é resultado de review, não impede dispatch de TODO."""
        assert Modifier.REVIEWED not in STOP_MODIFIERS

    def test_valores_das_labels_tem_prefixo_crewflow(self) -> None:
        for s in State:
            assert s.value.startswith("flow:"), s
        for m in Modifier:
            assert m.value.startswith("flow:"), m


class TestStateOrdering:
    def test_spec_vem_antes_de_done(self) -> None:
        assert State.BRIEFING < State.DONE

    def test_todo_vem_antes_de_review(self) -> None:
        assert State.DEVELOP_WAITING < State.REVIEW_WAITING

    def test_qa_vem_depois_de_review(self) -> None:
        assert State.QA_WAITING > State.REVIEW_WAITING

    def test_lista_de_estados_na_ordem_canonica(self) -> None:
        esperado = [
            State.BRIEFING, State.PLANNING_SPECS, State.PLANNING_REVIEW,
            State.DEVELOP_WAITING, State.DEVELOP_RUNNING,
            State.REVIEW_WAITING, State.REVIEW_APPROVED, State.REVIEW_REFUSED,
            State.QA_WAITING, State.QA_TESTING, State.QA_APPROVED, State.QA_REFUSED,
            State.DONE,
        ]
        assert list(State) == esperado


# ---------------------------------------------------------------------------
# parse_state
# ---------------------------------------------------------------------------

class TestParseState:
    def test_retorna_none_sem_label_de_estado(self) -> None:
        assert parse_state({"bug", "phase-1", "enhancement"}) is None

    def test_retorna_none_com_set_vazio(self) -> None:
        assert parse_state(set()) is None

    def test_extrai_estado_correto(self) -> None:
        assert parse_state({"flow:develop-running", "phase-1"}) is State.DEVELOP_RUNNING

    def test_ignora_labels_nao_relacionadas(self) -> None:
        labels = {"flow:qa-waiting", "flow:blocked", "documentation", "phase-1"}
        assert parse_state(labels) is State.QA_WAITING

    def test_dois_estados_lancam_erro(self) -> None:
        with pytest.raises(EstadoAmbiguo) as exc_info:
            parse_state({"flow:develop-running", "flow:qa-waiting"})
        assert "flow:develop-running" in str(exc_info.value)
        assert "flow:qa-waiting" in str(exc_info.value)

    def test_tres_estados_tambem_lancam_erro(self) -> None:
        with pytest.raises(EstadoAmbiguo):
            parse_state({"flow:briefing", "flow:develop-running", "flow:qa-waiting"})

    def test_hml_bypass_nao_e_estado(self) -> None:
        """hml-bypass é modificador, não substitui o estado."""
        resultado = parse_state({"flow:qa-waiting", "flow:blocked"})
        assert resultado is State.QA_WAITING

    def test_aceita_frozenset(self) -> None:
        labels = frozenset({"flow:planning-specs"})
        assert parse_state(labels) is State.PLANNING_SPECS


# ---------------------------------------------------------------------------
# parse_modifiers
# ---------------------------------------------------------------------------

class TestParseModifiers:
    def test_retorna_frozenset_vazio_sem_modificadores(self) -> None:
        assert parse_modifiers({"flow:develop-running", "phase-1"}) == frozenset()

    def test_extrai_blocked(self) -> None:
        result = parse_modifiers({"flow:blocked", "flow:develop-running"})
        assert Modifier.BLOCKED in result

    def test_extrai_multiplos_modificadores(self) -> None:
        labels = {"flow:blocked", "flow:review-running", "flow:merge-conflict", "documentation"}
        result = parse_modifiers(labels)
        assert result == frozenset({Modifier.BLOCKED, Modifier.REVIEWED, Modifier.MERGE_CONFLICT})

    def test_ignora_labels_desconhecidas(self) -> None:
        labels = {"flow:blocked", "something:unknown", "phase-1"}
        result = parse_modifiers(labels)
        assert result == frozenset({Modifier.BLOCKED})

    def test_retorna_frozenset(self) -> None:
        result = parse_modifiers({"flow:blocked"})
        assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# is_dispatchable
# ---------------------------------------------------------------------------

class TestIsDispatchable:
    def test_todo_sem_modificadores_e_despachavel(self) -> None:
        assert is_dispatchable(State.DEVELOP_WAITING, frozenset())

    def test_todo_com_blocked_nao_e_despachavel(self) -> None:
        """blocked tem prioridade sobre o estado gatilho."""
        assert not is_dispatchable(State.DEVELOP_WAITING, frozenset({Modifier.BLOCKED}))

    def test_todo_com_running_nao_e_despachavel(self) -> None:
        """No novo design, develop-running é um estado separado, não um modificador.
        develop-waiting com blocked não é despachável."""
        assert not is_dispatchable(State.DEVELOP_WAITING, frozenset({Modifier.BLOCKED}))

    def test_todo_com_merge_conflict_e_despachavel(self) -> None:
        """merge-conflict é tratado por cron separado, não bloqueia dispatch."""
        assert is_dispatchable(State.DEVELOP_WAITING, frozenset({Modifier.MERGE_CONFLICT}))

    def test_dev_nao_e_despachavel(self) -> None:
        assert not is_dispatchable(State.DEVELOP_RUNNING, frozenset())

    def test_spec_nao_e_despachavel(self) -> None:
        assert not is_dispatchable(State.BRIEFING, frozenset())

    def test_none_nao_e_despachavel(self) -> None:
        """issue fora da esteira."""
        assert not is_dispatchable(None, frozenset())

    def test_hml_bypass_nao_impede_dispatch(self) -> None:
        """flow:review-running é lock anti-loop, não para o dispatch de uma nova issue."""
        assert is_dispatchable(State.DEVELOP_WAITING, frozenset({Modifier.REVIEWED}))

    def test_reviewed_nao_impede_dispatch(self) -> None:
        """reviewed é lock anti-loop do review, não do todo."""
        assert is_dispatchable(State.DEVELOP_WAITING, frozenset({Modifier.REVIEWED}))

    def test_combinacao_de_stop_modifiers(self) -> None:
        mods = frozenset({Modifier.BLOCKED, Modifier.MERGE_CONFLICT})
        assert not is_dispatchable(State.DEVELOP_WAITING, mods)

    def test_done_nao_e_despachavel(self) -> None:
        assert not is_dispatchable(State.DONE, frozenset())


# ---------------------------------------------------------------------------
# can_transition
# ---------------------------------------------------------------------------

class TestCanTransition:
    def test_avanco_sequencial_valido(self) -> None:
        pares_validos = [
            (State.BRIEFING,         State.PLANNING_SPECS),
            (State.PLANNING_SPECS,   State.PLANNING_REVIEW),
            (State.PLANNING_REVIEW,  State.DEVELOP_WAITING),
            (State.DEVELOP_WAITING,  State.DEVELOP_RUNNING),
            (State.DEVELOP_RUNNING,  State.REVIEW_WAITING),
            (State.REVIEW_WAITING,   State.REVIEW_APPROVED),
            (State.REVIEW_APPROVED,  State.REVIEW_REFUSED),  # avanço de 1 passo
            (State.QA_WAITING,       State.QA_TESTING),
            (State.QA_TESTING,       State.QA_APPROVED),
        ]
        for de, para in pares_validos:
            assert can_transition(de, para), f"{de} → {para} deveria ser válido"

    def test_done_e_terminal(self) -> None:
        for outro in State:
            assert not can_transition(State.DONE, outro), (
                f"DONE → {outro} não deveria ser válido"
            )

    def test_retorno_para_dev_valido_apos_review(self) -> None:
        """Gate reprova: volta pro develop-waiting (gate humano, decisão do TL/dev)."""
        assert can_transition(State.REVIEW_WAITING, State.DEVELOP_WAITING)
        assert can_transition(State.REVIEW_REFUSED, State.DEVELOP_WAITING)

    def test_retorno_para_dev_valido_apos_qa(self) -> None:
        assert can_transition(State.QA_WAITING, State.DEVELOP_WAITING)
        assert can_transition(State.QA_REFUSED, State.DEVELOP_WAITING)

    def test_pulo_de_mais_de_um_passo_invalido(self) -> None:
        assert not can_transition(State.BRIEFING, State.DEVELOP_RUNNING)
        assert not can_transition(State.BRIEFING, State.DEVELOP_WAITING)
        assert not can_transition(State.PLANNING_SPECS, State.DEVELOP_RUNNING)

    def test_spec_nao_pode_voltar_para_dev(self) -> None:
        """DEV é antes de SPEC na order; spec → dev seria pular pra frente de 2."""
        assert not can_transition(State.BRIEFING, State.DEVELOP_RUNNING)

    def test_todo_nao_volta_para_dev_pois_ja_e_anterior(self) -> None:
        """TODO → DEV é avanço sequencial (válido), não retorno."""
        assert can_transition(State.DEVELOP_WAITING, State.DEVELOP_RUNNING)


# ---------------------------------------------------------------------------
# transition_state
# ---------------------------------------------------------------------------

class TestTransitionState:
    """transition_state() garante exclusividade mútua de estados."""

    def test_troca_estado_simples(self) -> None:
        """todo → dev: remove todo, adiciona dev."""
        labels = {"flow:develop-waiting", "flow:bug", "phase-1"}
        result = transition_state(labels, State.DEVELOP_RUNNING)
        assert "flow:develop-running" in result
        assert "flow:develop-waiting" not in result

    def test_preserva_modificadores(self) -> None:
        """Modificadores (running, blocked, etc.) são preservados."""
        labels = {"flow:develop-waiting", "flow:develop-running", "flow:bug"}
        result = transition_state(labels, State.DEVELOP_RUNNING)
        assert "flow:develop-running" in result
        assert "flow:develop-running" in result
        assert "flow:develop-waiting" not in result

    def test_preserva_labels_de_tipo(self) -> None:
        """Labels de tipo (bug, feature, hotfix, debt) são preservadas."""
        labels = {"flow:develop-waiting", "flow:bug", "flow:p2"}
        result = transition_state(labels, State.REVIEW_WAITING)
        assert "flow:bug" in result
        assert "flow:p2" in result
        assert "flow:review-waiting" in result

    def test_preserva_labels_externas(self) -> None:
        """Labels de outros sistemas (phase-1, documentation) são preservadas."""
        labels = {"flow:develop-running", "phase-1", "documentation", "flow:develop-running"}
        result = transition_state(labels, State.REVIEW_WAITING)
        assert "phase-1" in result
        assert "documentation" in result
        assert "flow:review-waiting" in result

    def test_remove_todos_os_estados_anteriores(self) -> None:
        """Caso de sobreposição (bug observado): develop-waiting + develop-running ao mesmo tempo → só review."""
        labels = {"flow:develop-waiting", "flow:develop-running", "flow:bug"}
        result = transition_state(labels, State.REVIEW_WAITING)
        # Apenas flow:review-waiting como estado
        estados_em_result = {lbl for lbl in result if lbl.startswith("flow:") and lbl in {s.value for s in State}}
        assert estados_em_result == {"flow:review-waiting"}
        # Tipo preservado (flow:bug não é estado)
        assert "flow:bug" in result
        # Estados anteriores removidos
        assert "flow:develop-waiting" not in result
        assert "flow:develop-running" not in result

    def test_todo_para_dev_nao_acumula_estados(self) -> None:
        """Transição todo→dev nunca deixa flow:develop-waiting na lista."""
        labels = {"flow:develop-waiting", "flow:develop-running", "flow:p1"}
        result = transition_state(labels, State.DEVELOP_RUNNING)
        assert parse_state(result) is State.DEVELOP_RUNNING

    def test_dev_para_review_nao_acumula_estados(self) -> None:
        """O bug original: dev→review nunca deixa flow:develop-running + flow:review-waiting juntos."""
        labels = {"flow:develop-running", "flow:develop-running", "flow:bug"}
        result = transition_state(labels, State.REVIEW_WAITING)
        assert parse_state(result) is State.REVIEW_WAITING

    def test_retorna_frozenset(self) -> None:
        result = transition_state({"flow:develop-waiting"}, State.DEVELOP_RUNNING)
        assert isinstance(result, frozenset)

    def test_funciona_com_frozenset_input(self) -> None:
        labels = frozenset({"flow:develop-waiting", "flow:bug"})
        result = transition_state(labels, State.DEVELOP_RUNNING)
        assert parse_state(result) is State.DEVELOP_RUNNING

    def test_exatamente_um_estado_no_resultado(self) -> None:
        """Invariante central: exatamente 1 estado no resultado."""
        state_values = {s.value for s in State}
        for de in State:
            for para in State:
                labels = {de.value, "flow:develop-running", "flow:bug", "phase-1"}
                result = transition_state(labels, para)
                estados = {lbl for lbl in result if lbl in state_values}
                assert len(estados) == 1, (
                    f"transition_state({de!r}, {para!r}) resultou em "
                    f"{len(estados)} estados: {estados!r}"
                )
                assert para.value in estados

    def test_sem_labels_de_estado_adiciona_novo(self) -> None:
        """Issue sem estado anterior recebe o novo estado."""
        labels: set[str] = {"flow:bug", "phase-1"}
        result = transition_state(labels, State.DEVELOP_WAITING)
        assert "flow:develop-waiting" in result
        assert "flow:bug" in result
