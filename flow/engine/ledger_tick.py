"""Motor ledger-driven do single-flow (frente 7).

Uma cron ÚNICA, zero-token no scan, cujo estado NÃO vem mais das labels do
GitHub e sim do ``RunLedger`` (SQLite local). A cada tick:

    1. ``ledger.active()``  — 1 query SQLite local (nada de ``gh issue list``)
    2. sem run ativa        — nada a fazer (``idle``)
    3. run ativa            — 1 ``provider.get_work_item`` para ler o estado REAL
                              da issue (existe PR? review pronto?), e então decidir
    4. estágio ainda rodando — deixa quieto (``waiting``)
    5. hora de agir          — dispara o estágio via o dispatcher e avança o ledger

Diferença central para o motor label-driven (``deployment._run_stage``): aqui o
custo de rede é **O(1) por tick** — uma leitura da task ativa — em vez de
O(estados x estágios). O modo paralelo legado não é tocado; este é um caminho
paralelo, opt-in, dirigido pelo banco.

Máquina de estados (linear, single-flow):

    BRIEFING → PLANNING_SPECS → PLANNING_REVIEW → DEVELOP_WAITING
             → REVIEW_WAITING → QA_WAITING → DONE

Estágios que o motor DISPARA ativamente (tem dispatcher): briefing, planning.
Estágios que o motor AGUARDA um sinal externo (trabalho de outra sessão / humano)
e então avança: develop, review, qa. O ``Dispatcher`` (Protocol) isola o disparo
real (worktree + sessão one-shot), que vive em ``deployment.py``; aqui só a
orquestração pura, testável sem I/O de rede via um dispatcher fake.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from flow.domain.run_ledger import RunLedger, RunStatus
from flow.domain.state import State

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Máquina de estados linear do single-flow
# ---------------------------------------------------------------------------

#: Próximo estado de cada estado, na sequência single-flow. Estados de "espera"
#: (develop/review/qa) avançam quando o sinal externo chega; estados ativos
#: (briefing/planning) avançam após o motor disparar o estágio.
_NEXT_STATE: dict[State, State] = {
    State.BRIEFING:        State.PLANNING_SPECS,
    State.PLANNING_SPECS:  State.PLANNING_REVIEW,
    State.PLANNING_REVIEW: State.DEVELOP_WAITING,
    State.DEVELOP_WAITING: State.REVIEW_WAITING,
    State.REVIEW_WAITING:  State.QA_WAITING,
    State.QA_WAITING:      State.DONE,
}

#: Estados terminais — liberam o slot para a próxima task.
_TERMINAL_STATES: frozenset[State] = frozenset({
    State.DONE,
    State.REVIEW_REFUSED,
    State.QA_REFUSED,
})

#: Estados em que o motor DISPARA um estágio ativamente (tem dispatcher).
#: Nos demais estados de "espera" (develop/review/qa) o motor só observa o
#: sinal externo e avança quando ele chega.
_ACTIVE_STAGES: frozenset[State] = frozenset({
    State.BRIEFING,
    State.PLANNING_SPECS,
})


# ---------------------------------------------------------------------------
# Porta de disparo (isola o I/O real do motor)
# ---------------------------------------------------------------------------

@runtime_checkable
class Dispatcher(Protocol):
    """Contrato de disparo de um estágio.

    A implementação real (``deployment.py``) abre worktree + sessão one-shot.
    Nos testes, um fake registra as chamadas sem tocar rede.
    """

    def dispatch_stage(self, repo: str, issue: dict, stage: State) -> object:
        """Dispara o trabalho do ``stage`` para a issue.

        Retorna um valor TRUTHY quando o disparo aconteceu (idealmente o id da
        sessão criada, uma ``str``, que o motor grava como ``stage_session``),
        e um valor FALSY (``False``/``None``/``""``) quando foi abortado (issue
        fechada, lock já existe, etc.) — o motor não avança nem prende sessão
        num disparo abortado.
        """
        ...


@runtime_checkable
class StateReader(Protocol):
    """Lê o estado REAL da issue no provedor (1 chamada por tick)."""

    def read_state(self, repo: str, task_key: str) -> State | None:
        """Retorna o ``State`` atual da issue, ou ``None`` se indeterminado."""
        ...


# ---------------------------------------------------------------------------
# Motor
# ---------------------------------------------------------------------------

def _task_number(task_key: str) -> int | None:
    """Extrai o número da issue de um ``task_key`` ("owner/repo#42" → 42)."""
    if "#" in task_key:
        tail = task_key.rsplit("#", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


def tick(
    ledger: RunLedger,
    dispatcher: Dispatcher,
    reader: StateReader,
    *,
    stage_running: bool = False,
) -> str:
    """Executa um ciclo do motor ledger-driven.

    Args:
        ledger:        fonte de verdade do estado (SQLite local).
        dispatcher:    dispara o estágio ativo (worktree + sessão).
        reader:        lê o estado real da issue no provedor.
        stage_running: ``True`` se a sessão one-shot do estágio atual ainda
                       está viva (o caller detecta; o motor só respeita).

    Returns:
        Uma string de diagnóstico do que o tick fez:
        ``idle`` | ``waiting:<stage>`` | ``advanced:<de>-><para>``
        | ``dispatched:<stage>`` | ``released:<status>`` | ``dispatch-aborted:<stage>``
    """
    run = ledger.active()
    if run is None:
        return "idle"

    # Sessão do estágio atual ainda viva → não faz nada neste tick.
    if stage_running:
        return f"waiting:{run.current_stage}"

    try:
        current = State(run.current_stage)
    except ValueError:
        logger.error(
            "ledger_tick: estágio desconhecido no ledger: %r (task %s)",
            run.current_stage, run.task_key,
        )
        return f"waiting:{run.current_stage}"

    # Estado terminal já registrado — libera o slot.
    if current in _TERMINAL_STATES:
        status = RunStatus.DONE if current is State.DONE else RunStatus.REFUSED
        ledger.release(run.task_key, status)
        return f"released:{status.value}"

    # Lê o estado REAL da issue (1 chamada de rede). Se o provedor já reflete um
    # estado terminal ou refused, respeita-o.
    real = reader.read_state(run.repo, run.task_key)
    if real is not None and real in _TERMINAL_STATES:
        status = RunStatus.DONE if real is State.DONE else RunStatus.REFUSED
        ledger.release(run.task_key, status)
        return f"released:{status.value}"

    # Estágio ATIVO (briefing/planning): o motor dispara a sessão UMA vez e
    # espera ela terminar antes de avançar. Isso evita o Gap C (enxame de
    # sessões duplicadas): sem prender a sessão criada, cada tick de 1min
    # abria uma nova. Agora:
    #   - sem sessão registrada ainda   → dispara, grava stage_session, NÃO avança
    #   - sessão registrada e ainda viva → aguarda (stage_running cobre isso acima)
    #   - sessão registrada mas já morta → avança para o próximo estágio
    if current in _ACTIVE_STAGES:
        # Sessão do estágio já foi criada num tick anterior e terminou
        # (stage_running=False chegou até aqui) → o trabalho do estágio acabou;
        # avança para o próximo estágio, limpando o marcador de sessão.
        if run.stage_session:
            nxt = _NEXT_STATE[current]
            ledger.advance(run.task_key, nxt.value, stage_session=None)
            return f"advanced:{current.value}->{nxt.value}"

        # Primeira vez neste estágio: dispara a sessão e a PRENDE no ledger.
        number = _task_number(run.task_key)
        issue = {"number": number, "key": run.task_key}
        session = dispatcher.dispatch_stage(run.repo, issue, current)
        if not session:
            return f"dispatch-aborted:{current.value}"
        # Grava a sessão SEM trocar de estágio: o próximo tick vê stage_session
        # preenchido e (enquanto viva) aguarda; quando morta, avança.
        session_id = session if isinstance(session, str) else f"{current.value}:{number}"
        ledger.advance(run.task_key, current.value, stage_session=session_id)
        return f"dispatched:{current.value}"

    # Estágio de ESPERA (develop/review/qa): avança quando o estado real da
    # issue já passou do estágio atual (sinal externo chegou). Caso contrário,
    # aguarda — sem gastar token.
    nxt_wait = _NEXT_STATE.get(current)
    if nxt_wait is None:
        return f"waiting:{current.value}"

    if real is not None and real >= nxt_wait:
        ledger.advance(run.task_key, real.value)
        return f"advanced:{current.value}->{real.value}"

    return f"waiting:{current.value}"
