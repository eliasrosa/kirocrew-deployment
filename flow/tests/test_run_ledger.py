"""Testes do RunLedger (single-flow) — SQLite local, sem mock.

Cobrem a regra central: apenas UMA run ativa por vez (single-flow lock),
transições de estágio, contagem de tentativas e liberação do slot.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from flow.domain.run_ledger import (
    RunLedger,
    RunStatus,
    SqliteRunLedger,
)


@pytest.fixture()
def ledger(tmp_path: Path) -> Iterator[SqliteRunLedger]:
    lg = SqliteRunLedger(squad_id="test", data_dir=tmp_path)
    yield lg
    lg.close()


# ---------------------------------------------------------------------------
# Conformidade com a interface
# ---------------------------------------------------------------------------

def test_sqlite_satisfies_protocol(ledger: SqliteRunLedger) -> None:
    assert isinstance(ledger, RunLedger)


# ---------------------------------------------------------------------------
# claim / active — o single-flow lock
# ---------------------------------------------------------------------------

def test_claim_empty_slot_succeeds(ledger: SqliteRunLedger) -> None:
    assert ledger.claim("owner/repo#1", "repo", "flow:briefing") is True
    active = ledger.active()
    assert active is not None
    assert active.task_key == "owner/repo#1"
    assert active.current_stage == "flow:briefing"
    assert active.status is RunStatus.RUNNING


def test_claim_second_task_while_active_fails(ledger: SqliteRunLedger) -> None:
    assert ledger.claim("owner/repo#1", "repo", "flow:briefing") is True
    # slot ocupado → segunda task recusada
    assert ledger.claim("owner/repo#2", "repo", "flow:briefing") is False
    active = ledger.active()
    assert active is not None
    assert active.task_key == "owner/repo#1"


def test_claim_same_task_is_idempotent(ledger: SqliteRunLedger) -> None:
    assert ledger.claim("owner/repo#1", "repo", "flow:briefing") is True
    assert ledger.claim("owner/repo#1", "repo", "flow:briefing") is True


def test_active_is_none_on_empty_ledger(ledger: SqliteRunLedger) -> None:
    assert ledger.active() is None


# ---------------------------------------------------------------------------
# advance / bump_attempt
# ---------------------------------------------------------------------------

def test_advance_changes_stage_and_resets_attempts(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:briefing")
    ledger.bump_attempt("owner/repo#1")
    ledger.bump_attempt("owner/repo#1")
    ledger.advance("owner/repo#1", "flow:planning-specs", stage_session="sess-123")
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.current_stage == "flow:planning-specs"
    assert run.stage_session == "sess-123"
    assert run.attempts == 0


def test_bump_attempt_increments(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:briefing")
    assert ledger.bump_attempt("owner/repo#1") == 1
    assert ledger.bump_attempt("owner/repo#1") == 2


# ---------------------------------------------------------------------------
# release — libera o slot para a próxima task
# ---------------------------------------------------------------------------

def test_release_frees_slot(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:briefing")
    ledger.release("owner/repo#1", RunStatus.DONE)
    assert ledger.active() is None
    # agora uma nova task pode ser presa
    assert ledger.claim("owner/repo#2", "repo", "flow:briefing") is True


def test_released_run_keeps_terminal_status(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:briefing")
    ledger.release("owner/repo#1", RunStatus.REFUSED)
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.status is RunStatus.REFUSED


@pytest.mark.parametrize(
    "status", [RunStatus.DONE, RunStatus.REFUSED, RunStatus.FAILED]
)
def test_all_terminal_statuses_free_the_slot(
    ledger: SqliteRunLedger, status: RunStatus
) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:briefing")
    ledger.release("owner/repo#1", status)
    assert ledger.active() is None


# ---------------------------------------------------------------------------
# all — visão de tudo (base da futura visão OTL)
# ---------------------------------------------------------------------------

def test_all_returns_every_run(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:briefing")
    ledger.release("owner/repo#1", RunStatus.DONE)
    ledger.claim("owner/repo#2", "repo", "flow:briefing")
    keys = {r.task_key for r in ledger.all()}
    assert keys == {"owner/repo#1", "owner/repo#2"}


# ---------------------------------------------------------------------------
# persistência entre conexões (mesmo banco)
# ---------------------------------------------------------------------------

def test_state_persists_across_reopen(tmp_path: Path) -> None:
    lg1 = SqliteRunLedger(squad_id="persist", data_dir=tmp_path)
    lg1.claim("owner/repo#9", "repo", "flow:planning-specs")
    lg1.close()

    lg2 = SqliteRunLedger(squad_id="persist", data_dir=tmp_path)
    active = lg2.active()
    assert active is not None
    assert active.task_key == "owner/repo#9"
    assert active.current_stage == "flow:planning-specs"
    lg2.close()
