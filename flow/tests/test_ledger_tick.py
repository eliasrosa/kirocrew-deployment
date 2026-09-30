"""Testes do motor ledger-driven do single-flow (frente 7).

Orquestração pura: usa o SqliteRunLedger real (tmp_path) e fakes de
Dispatcher/StateReader — sem I/O de rede. Cobre cada ramo do tick():
idle, waiting (sessão viva), dispatch de estágio ativo, avanço por sinal
externo, e release em estado terminal.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from flow.domain.run_ledger import RunStatus, SqliteRunLedger
from flow.domain.state import State
from flow.engine.ledger_tick import tick


@pytest.fixture()
def ledger(tmp_path: Path) -> Iterator[SqliteRunLedger]:
    lg = SqliteRunLedger(squad_id="test", data_dir=tmp_path)
    yield lg
    lg.close()


class FakeDispatcher:
    """Registra os disparos; controla se aceita ou aborta."""

    def __init__(self, accept: bool = True) -> None:
        self.accept = accept
        self.calls: list[tuple[str, dict, State]] = []

    def dispatch_stage(self, repo: str, issue: dict, stage: State) -> bool:
        self.calls.append((repo, issue, stage))
        return self.accept


class FakeReader:
    """Devolve um estado real fixo (ou None)."""

    def __init__(self, state: State | None = None) -> None:
        self.state = state
        self.calls: list[tuple[str, str]] = []

    def read_state(self, repo: str, task_key: str) -> State | None:
        self.calls.append((repo, task_key))
        return self.state


# ---------------------------------------------------------------------------
# idle / waiting
# ---------------------------------------------------------------------------

def test_idle_quando_nao_ha_run_ativa(ledger: SqliteRunLedger) -> None:
    result = tick(ledger, FakeDispatcher(), FakeReader())
    assert result == "idle"


def test_waiting_quando_sessao_do_estagio_esta_viva(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "owner/repo", State.BRIEFING.value)
    disp = FakeDispatcher()
    result = tick(ledger, disp, FakeReader(), stage_running=True)
    assert result == f"waiting:{State.BRIEFING.value}"
    assert disp.calls == []  # não dispara enquanto a sessão está viva


# ---------------------------------------------------------------------------
# estágios ativos (briefing/planning) — dispara UMA vez, avança quando a
# sessão termina (Gap C: não redispara enquanto a sessão está viva)
# ---------------------------------------------------------------------------

def test_dispara_briefing_prende_sessao_sem_avancar(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "owner/repo", State.BRIEFING.value)
    disp = FakeDispatcher(accept=True)
    # 1º tick: dispara e PRENDE a sessão, SEM avançar (fica em briefing).
    result = tick(ledger, disp, FakeReader())
    assert result == f"dispatched:{State.BRIEFING.value}"
    assert len(disp.calls) == 1
    repo, issue, stage = disp.calls[0]
    assert repo == "owner/repo"
    assert issue["number"] == 1
    assert stage is State.BRIEFING
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.current_stage == State.BRIEFING.value  # NÃO avançou ainda
    assert run.stage_session  # sessão prendida

    # Enquanto a sessão vive (stage_running=True), NÃO redispara.
    result2 = tick(ledger, disp, FakeReader(), stage_running=True)
    assert result2 == f"waiting:{State.BRIEFING.value}"
    assert len(disp.calls) == 1  # nenhum disparo novo (Gap C corrigido)


def test_avanca_briefing_quando_sessao_terminou(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "owner/repo", State.BRIEFING.value)
    disp = FakeDispatcher(accept=True)
    tick(ledger, disp, FakeReader())  # dispara e prende
    # 2º tick com stage_running=False (sessão morreu) → avança.
    result = tick(ledger, disp, FakeReader())
    assert result == f"advanced:{State.BRIEFING.value}->{State.PLANNING_SPECS.value}"
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.current_stage == State.PLANNING_SPECS.value
    assert not run.stage_session  # marcador limpo ao avançar
    assert len(disp.calls) == 1  # não redisparou briefing


def test_dispara_planning_prende_sessao_sem_avancar(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#2", "owner/repo", State.PLANNING_SPECS.value)
    disp = FakeDispatcher(accept=True)
    result = tick(ledger, disp, FakeReader())
    assert result == f"dispatched:{State.PLANNING_SPECS.value}"
    run = ledger.get("owner/repo#2")
    assert run is not None
    assert run.current_stage == State.PLANNING_SPECS.value  # não avançou
    assert run.stage_session
    # sessão terminou → 2º tick avança
    result2 = tick(ledger, disp, FakeReader())
    assert result2 == f"advanced:{State.PLANNING_SPECS.value}->{State.PLANNING_REVIEW.value}"
    run2 = ledger.get("owner/repo#2")
    assert run2 is not None
    assert run2.current_stage == State.PLANNING_REVIEW.value


def test_dispatch_abortado_nao_avanca_ledger(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#3", "owner/repo", State.BRIEFING.value)
    disp = FakeDispatcher(accept=False)  # dispatcher aborta (issue closed / lock)
    result = tick(ledger, disp, FakeReader())
    assert result == f"dispatch-aborted:{State.BRIEFING.value}"
    run = ledger.get("owner/repo#3")
    assert run is not None
    assert run.current_stage == State.BRIEFING.value  # não avançou


# ---------------------------------------------------------------------------
# estágios de espera (develop/review/qa) — avança por sinal externo
# ---------------------------------------------------------------------------

def test_aguarda_quando_sinal_externo_nao_chegou(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#4", "owner/repo", State.DEVELOP_WAITING.value)
    # estado real ainda no mesmo estágio → aguarda
    reader = FakeReader(state=State.DEVELOP_WAITING)
    result = tick(ledger, FakeDispatcher(), reader)
    assert result == f"waiting:{State.DEVELOP_WAITING.value}"


def test_avanca_quando_estado_real_passou_do_estagio(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#5", "owner/repo", State.DEVELOP_WAITING.value)
    # dev terminou → PR aberta → estado real já é review-waiting
    reader = FakeReader(state=State.REVIEW_WAITING)
    result = tick(ledger, FakeDispatcher(), reader)
    assert result == f"advanced:{State.DEVELOP_WAITING.value}->{State.REVIEW_WAITING.value}"
    run = ledger.get("owner/repo#5")
    assert run is not None
    assert run.current_stage == State.REVIEW_WAITING.value


# ---------------------------------------------------------------------------
# terminais — libera o slot
# ---------------------------------------------------------------------------

def test_release_quando_ledger_ja_em_done(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#6", "owner/repo", State.DONE.value)
    result = tick(ledger, FakeDispatcher(), FakeReader())
    assert result == f"released:{RunStatus.DONE.value}"
    assert ledger.active() is None  # slot liberado


def test_release_quando_estado_real_e_refused(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#7", "owner/repo", State.REVIEW_WAITING.value)
    reader = FakeReader(state=State.REVIEW_REFUSED)
    result = tick(ledger, FakeDispatcher(), reader)
    assert result == f"released:{RunStatus.REFUSED.value}"
    assert ledger.active() is None


def test_release_libera_slot_para_proxima_task(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#8", "owner/repo", State.DONE.value)
    tick(ledger, FakeDispatcher(), FakeReader())
    # agora uma nova task pode ser presa
    assert ledger.claim("owner/repo#9", "owner/repo", State.BRIEFING.value) is True
    active = ledger.active()
    assert active is not None
    assert active.task_key == "owner/repo#9"


# ---------------------------------------------------------------------------
# robustez
# ---------------------------------------------------------------------------

def test_estagio_desconhecido_no_ledger_nao_quebra(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#10", "owner/repo", "flow:estado-inexistente")
    result = tick(ledger, FakeDispatcher(), FakeReader())
    assert result.startswith("waiting:")


# ---------------------------------------------------------------------------
# item B — estados órfãos que travavam o motor (REVIEW_APPROVED, QA_TESTING,
#           QA_APPROVED agora têm entrada em _NEXT_STATE)
# ---------------------------------------------------------------------------

def test_review_approved_avanca_para_qa_waiting(ledger: SqliteRunLedger) -> None:
    """REVIEW_APPROVED: antes não estava em _NEXT_STATE → waiting eterno.
    Avança quando o reader retorna QA_WAITING ou além (o sinal externo do QA)."""
    ledger.claim("owner/repo#20", "owner/repo", State.REVIEW_APPROVED.value)
    # Reader precisa retornar >= QA_WAITING para o avanço ocorrer
    reader = FakeReader(state=State.QA_WAITING)
    result = tick(ledger, FakeDispatcher(), reader)
    assert result == f"advanced:{State.REVIEW_APPROVED.value}->{State.QA_WAITING.value}"


def test_review_approved_aguarda_enquanto_sinal_nao_chegou(ledger: SqliteRunLedger) -> None:
    """REVIEW_APPROVED sem sinal externo → waiting (não trava mais, só aguarda)."""
    ledger.claim("owner/repo#20b", "owner/repo", State.REVIEW_APPROVED.value)
    reader = FakeReader(state=State.REVIEW_APPROVED)  # ainda no mesmo estado
    result = tick(ledger, FakeDispatcher(), reader)
    # Não trava (tem _NEXT_STATE), aguarda o sinal externo chegar
    assert result == f"waiting:{State.REVIEW_APPROVED.value}"


def test_qa_testing_avanca_para_qa_approved(ledger: SqliteRunLedger) -> None:
    """QA_TESTING: estava em _NEXT_STATE como None → waiting eterno ao gravar
    manualmente. Agora avança para QA_APPROVED."""
    ledger.claim("owner/repo#21", "owner/repo", State.QA_TESTING.value)
    reader = FakeReader(state=State.QA_APPROVED)
    result = tick(ledger, FakeDispatcher(), reader)
    assert result == f"advanced:{State.QA_TESTING.value}->{State.QA_APPROVED.value}"


def test_qa_approved_avanca_para_done(ledger: SqliteRunLedger) -> None:
    """QA_APPROVED: antes não estava em _NEXT_STATE → waiting eterno.
    Agora avança para DONE e libera o slot."""
    ledger.claim("owner/repo#22", "owner/repo", State.QA_APPROVED.value)
    reader = FakeReader(state=State.DONE)
    result = tick(ledger, FakeDispatcher(), reader)
    # DONE é terminal → released
    assert result == f"released:{RunStatus.DONE.value}"
    assert ledger.active() is None


def test_estado_orfao_nao_trava_mais(ledger: SqliteRunLedger) -> None:
    """Confirma que QA_WAITING não trava mais com reader retornando QA_APPROVED.
    DONE no reader libera diretamente (é terminal — não passa pelo _NEXT_STATE).
    O caminho QA_WAITING→QA_APPROVED só ocorre quando o reader retorna QA_APPROVED."""
    ledger.claim("owner/repo#23", "owner/repo", State.QA_WAITING.value)
    # Reader retorna QA_APPROVED (sinal: QA aprovou, ainda não DONE)
    reader = FakeReader(state=State.QA_APPROVED)
    result = tick(ledger, FakeDispatcher(), reader)
    # QA_APPROVED >= QA_APPROVED → avança de QA_WAITING para QA_APPROVED
    assert result == f"advanced:{State.QA_WAITING.value}->{State.QA_APPROVED.value}"
    # Tick seguinte: ledger em QA_APPROVED, reader retorna DONE (terminal) → released
    reader2 = FakeReader(state=State.DONE)
    result2 = tick(ledger, FakeDispatcher(), reader2)
    assert result2 == f"released:{RunStatus.DONE.value}"
    assert ledger.active() is None
