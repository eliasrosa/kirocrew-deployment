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


# ---------------------------------------------------------------------------
# Estado de review no ledger (migração do comentário KIRO-FLOW-STATE)
# ---------------------------------------------------------------------------

def test_review_result_defaults_are_empty(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:review-waiting")
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.review_sha == ""
    assert run.review_iterations == 0
    assert run.review_approved is None
    assert run.approvals == {}


def test_set_review_result_persists_sha_and_verdict(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:review-waiting")
    ledger.set_review_result("owner/repo#1", approved=True, sha="abc1234")
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.review_approved is True
    assert run.review_sha == "abc1234"


def test_set_review_result_refused(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:review-waiting")
    ledger.set_review_result("owner/repo#1", approved=False, sha="def5678")
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.review_approved is False
    assert run.review_sha == "def5678"


def test_review_sha_anti_loop_readback(ledger: SqliteRunLedger) -> None:
    # O reviewer grava o SHA analisado; um tick posterior lê do ledger
    # (não do comentário) para não re-analisar o mesmo commit.
    ledger.claim("owner/repo#1", "repo", "flow:review-waiting")
    ledger.set_review_result("owner/repo#1", approved=True, sha="sha-HEAD")
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.review_sha == "sha-HEAD"  # anti-loop: mesmo SHA → não re-analisa


def test_bump_review_iteration_increments(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:review-waiting")
    assert ledger.bump_review_iteration("owner/repo#1") == 1
    assert ledger.bump_review_iteration("owner/repo#1") == 2
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.review_iterations == 2


def test_add_approval_records_gate(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:briefing")
    ledger.add_approval("owner/repo#1", "gate-tl", "@elias", "2026-09-30")
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.approvals == {"gate-tl": "@elias|2026-09-30"}


def test_add_multiple_approvals_accumulate(ledger: SqliteRunLedger) -> None:
    ledger.claim("owner/repo#1", "repo", "flow:briefing")
    ledger.add_approval("owner/repo#1", "gate-tl", "@elias", "2026-09-30")
    ledger.add_approval("owner/repo#1", "gate-qa", "@ana", "2026-10-01")
    run = ledger.get("owner/repo#1")
    assert run is not None
    assert run.approvals["gate-tl"] == "@elias|2026-09-30"
    assert run.approvals["gate-qa"] == "@ana|2026-10-01"


def test_review_state_persists_across_reopen(tmp_path: Path) -> None:
    lg1 = SqliteRunLedger(squad_id="review-persist", data_dir=tmp_path)
    lg1.claim("owner/repo#7", "repo", "flow:review-waiting")
    lg1.set_review_result("owner/repo#7", approved=False, sha="commit-x")
    lg1.bump_review_iteration("owner/repo#7")
    lg1.add_approval("owner/repo#7", "gate-tl", "@elias", "2026-09-30")
    lg1.close()

    lg2 = SqliteRunLedger(squad_id="review-persist", data_dir=tmp_path)
    run = lg2.get("owner/repo#7")
    assert run is not None
    assert run.review_approved is False
    assert run.review_sha == "commit-x"
    assert run.review_iterations == 1
    assert run.approvals == {"gate-tl": "@elias|2026-09-30"}
    lg2.close()


# ---------------------------------------------------------------------------
# Migração idempotente: banco v1 (sem colunas de review) → adiciona colunas
# ---------------------------------------------------------------------------

def test_migration_adds_columns_to_v1_database(tmp_path: Path) -> None:
    import sqlite3

    # Cria um banco no schema ANTIGO (v1), sem as colunas de review.
    db_path = tmp_path / "run_ledger_legacy.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE run_ledger (
            task_key        TEXT PRIMARY KEY,
            repo            TEXT NOT NULL,
            current_stage   TEXT NOT NULL,
            stage_session   TEXT NULL,
            started_at      TEXT NOT NULL DEFAULT (datetime('now')),
            last_transition TEXT NOT NULL DEFAULT (datetime('now')),
            attempts        INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'running'
        )
        """
    )
    conn.execute(
        "INSERT INTO run_ledger (task_key, repo, current_stage) VALUES (?, ?, ?)",
        ("owner/repo#42", "repo", "flow:review-waiting"),
    )
    conn.commit()
    conn.close()

    # Abre com SqliteRunLedger apontando para o mesmo arquivo (data_dir + squad_id).
    lg = SqliteRunLedger(squad_id="legacy", data_dir=tmp_path)
    # A migração deve ter adicionado as colunas; a row antiga lê com defaults.
    run = lg.get("owner/repo#42")
    assert run is not None
    assert run.review_sha == ""
    assert run.review_iterations == 0
    assert run.review_approved is None
    assert run.approvals == {}
    # E os métodos novos funcionam sobre a row migrada.
    lg.set_review_result("owner/repo#42", approved=True, sha="new-sha")
    assert lg.get("owner/repo#42").review_sha == "new-sha"  # type: ignore[union-attr]
    lg.close()
