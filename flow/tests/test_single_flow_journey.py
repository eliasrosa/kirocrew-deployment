"""Teste de JORNADA end-to-end do single-flow (motor ledger-driven).

Diferente de ``test_ledger_tick.py`` (que prova cada RAMO do ``tick()``
isoladamente) e de ``test_e2e_integration.py`` (que prova o pipeline
label-driven estágio a estágio), este teste simula UMA task percorrendo o
fluxo INTEIRO de ponta a ponta:

    briefing → planning-specs → planning-review → develop
             → review → qa → done

Tudo mocado, zero I/O de rede:
  - ``SqliteRunLedger`` real (tmp_path) é a fonte de verdade do estado.
  - ``JourneyDispatcher`` registra os disparos dos estágios ATIVOS
    (briefing/planning) sem abrir worktree nem sessão.
  - ``JourneyReader`` simula o "trabalho externo pronto": a cada passo dos
    estágios de ESPERA (develop/review/qa) devolve o próximo estado real,
    como se o dev/reviewer/QA tivessem concluído.

O objetivo é um teste de regressão que garante que a máquina de estados
encadeia corretamente todas as transições até o terminal — o gap que os
testes por-ramo não cobrem.
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path

import pytest

from flow.domain.run_ledger import RunStatus, SqliteRunLedger
from flow.domain.state import State
from flow.engine.ledger_tick import tick

TASK = "owner/api-gateway2#42"
REPO = "owner/api-gateway2"

# Sequência canônica que uma task feliz percorre no single-flow.
HAPPY_PATH: list[State] = [
    State.BRIEFING,
    State.PLANNING_SPECS,
    State.PLANNING_REVIEW,
    State.DEVELOP_WAITING,
    State.REVIEW_WAITING,
    State.QA_WAITING,
    State.DONE,
]

# Estágios que o motor dispara ativamente (têm dispatcher).
_ACTIVE = {State.BRIEFING, State.PLANNING_SPECS}

# Estados terminais que o motor libera direto (release, não advance).
_TERMINAL = {State.DONE, State.REVIEW_REFUSED, State.QA_REFUSED}


@pytest.fixture()
def ledger(tmp_path: Path) -> Iterator[SqliteRunLedger]:
    lg = SqliteRunLedger(squad_id="journey", data_dir=tmp_path)
    yield lg
    lg.close()


class JourneyDispatcher:
    """Aceita todo disparo e registra a sequência de estágios disparados."""

    def __init__(self) -> None:
        self.dispatched: list[State] = []

    def dispatch_stage(self, repo: str, issue: dict, stage: State) -> bool:
        assert repo == REPO
        assert issue["key"] == TASK
        assert issue["number"] == 42
        self.dispatched.append(stage)
        return True


class JourneyReader:
    """Simula o mundo externo.

    Para os estágios de ESPERA (develop/review/qa), o teste avança o
    ``next_real`` ANTES do tick — como se o trabalho externo tivesse concluído
    e a issue já refletisse o próximo estado. Para os estágios ativos, o valor
    é irrelevante (o motor dispara sem ler o estado de espera), então mantemos
    o estado atual.
    """

    def __init__(self) -> None:
        self.next_real: State | None = None
        self.reads: int = 0

    def read_state(self, repo: str, task_key: str) -> State | None:
        self.reads += 1
        return self.next_real


def test_task_percorre_o_fluxo_inteiro_ate_done(ledger: SqliteRunLedger) -> None:
    """Uma task presa em BRIEFING chega a DONE percorrendo todos os estágios."""
    assert ledger.claim(TASK, REPO, State.BRIEFING.value) is True

    disp = JourneyDispatcher()
    reader = JourneyReader()
    trail: list[tuple[str, str]] = []  # (estado_antes, resultado_do_tick)

    # Percorre os pares (atual → próximo) da HAPPY_PATH. O último (DONE) é o
    # release, tratado dentro do loop quando nxt é terminal.
    for current, nxt in pairwise(HAPPY_PATH):
        run = ledger.active()
        assert run is not None, f"slot liberado cedo demais em {current.value}"
        assert run.current_stage == current.value

        if current in _ACTIVE:
            # Estágio ativo: o motor dispara e avança sozinho.
            reader.next_real = current  # irrelevante, mas realista
            result = tick(ledger, disp, reader)
            assert result == f"dispatched:{current.value}", (
                f"esperava dispatch em {current.value}, veio {result}"
            )
        elif nxt in _TERMINAL:
            # Última espera (qa-waiting → done): o estado real terminal é
            # detectado ANTES do avanço, então o motor libera o slot direto.
            # O resultado é um release, não um advance — e o slot fica livre.
            reader.next_real = nxt
            result = tick(ledger, disp, reader)
            assert result == f"released:{RunStatus.DONE.value}", (
                f"esperava release em {current.value}->{nxt.value}, veio {result}"
            )
            trail.append((current.value, result))
            break
        else:
            # Estágio de espera intermediário: simula o sinal externo (trabalho
            # pronto) deixando o estado real já no PRÓXIMO estágio.
            reader.next_real = nxt
            result = tick(ledger, disp, reader)
            assert result == f"advanced:{current.value}->{nxt.value}", (
                f"esperava advance {current.value}->{nxt.value}, veio {result}"
            )

        trail.append((current.value, result))
        # Após o tick (não-terminal), o ledger deve refletir o próximo estágio.
        run_after = ledger.get(TASK)
        assert run_after is not None
        assert run_after.current_stage == nxt.value, (
            f"após {current.value}, ledger deveria estar em {nxt.value}, "
            f"está em {run_after.current_stage}"
        )

    # Após a jornada completa, o slot foi liberado no passo terminal.
    assert ledger.active() is None, "slot deveria estar liberado após DONE"

    # Os dois estágios ativos foram disparados, na ordem certa.
    assert disp.dispatched == [State.BRIEFING, State.PLANNING_SPECS]

    # A trilha completa bate com a sequência esperada de transições.
    assert [t[0] for t in trail] == [
        State.BRIEFING.value,
        State.PLANNING_SPECS.value,
        State.PLANNING_REVIEW.value,
        State.DEVELOP_WAITING.value,
        State.REVIEW_WAITING.value,
        State.QA_WAITING.value,
    ]


def test_jornada_para_em_review_refused_e_libera_slot(ledger: SqliteRunLedger) -> None:
    """Se o reviewer reprova, a task termina em REFUSED e o slot é liberado."""
    assert ledger.claim(TASK, REPO, State.REVIEW_WAITING.value) is True

    reader = JourneyReader()
    reader.next_real = State.REVIEW_REFUSED  # reviewer reprovou

    result = tick(ledger, JourneyDispatcher(), reader)
    assert result == f"released:{RunStatus.REFUSED.value}"
    assert ledger.active() is None


def test_jornada_aguarda_enquanto_trabalho_externo_nao_conclui(
    ledger: SqliteRunLedger,
) -> None:
    """Em estágio de espera, sem sinal externo, o tick não avança nem gasta ação."""
    assert ledger.claim(TASK, REPO, State.DEVELOP_WAITING.value) is True

    reader = JourneyReader()
    reader.next_real = State.DEVELOP_WAITING  # dev ainda não terminou

    disp = JourneyDispatcher()
    # Vários ticks seguidos sem sinal externo → sempre waiting, nunca avança.
    for _ in range(3):
        result = tick(ledger, disp, reader)
        assert result == f"waiting:{State.DEVELOP_WAITING.value}"

    run = ledger.get(TASK)
    assert run is not None
    assert run.current_stage == State.DEVELOP_WAITING.value
    assert disp.dispatched == []  # nenhum dispatch em estágio de espera


def test_jornada_respeita_sessao_viva_do_estagio(ledger: SqliteRunLedger) -> None:
    """Enquanto a sessão one-shot do estágio ativo está viva, o motor não redispatcha."""
    assert ledger.claim(TASK, REPO, State.BRIEFING.value) is True

    disp = JourneyDispatcher()
    # sessão viva → waiting, sem novo disparo
    result = tick(ledger, disp, JourneyReader(), stage_running=True)
    assert result == f"waiting:{State.BRIEFING.value}"
    assert disp.dispatched == []

    # sessão morreu → dispara normalmente
    result = tick(ledger, disp, JourneyReader())
    assert result == f"dispatched:{State.BRIEFING.value}"
    assert disp.dispatched == [State.BRIEFING]
