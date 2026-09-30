"""RunLedger — estado local do modo single-flow.

Fonte de verdade do "onde a task está agora" no modo single-flow, separada das
labels ``flow:*`` (que continuam sendo aplicadas para auditoria no provedor).

Duas camadas:

    RunLedger      — a interface (Protocol) que o motor usa
    SqliteRunLedger — implementação local zero-dependência (agora)

DECISÃO DE DESIGN: o acesso ao estado fica atrás de uma interface para que uma
frente futura (visão OTL, store global compartilhado entre squads) seja apenas
uma NOVA implementação da mesma interface — o motor não muda. Mesmo princípio
hexagonal dos adapters de issue provider.

Regra do single-flow: apenas UMA task fica presa (``status == RUNNING``) por vez.
``claim()`` só tem sucesso se não houver run ativo. O tick empurra essa task
estágio a estágio até ``release()``.

Este módulo faz I/O de disco (SQLite) apenas na implementação; a interface é pura.
Segue o padrão de ``flow/scan/cache.py``: SQLite stdlib, schema idempotente,
banco por squad em ``<data_dir>/run_ledger_<squad_id>.db``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Tipos de domínio
# ---------------------------------------------------------------------------

class RunStatus(StrEnum):
    """Situação de uma run no ledger."""

    RUNNING  = "running"   # task presa e sendo empurrada pelo single-flow
    DONE      = "done"     # concluída (chegou em flow:done)
    REFUSED   = "refused"  # parou num gate humano (review/qa refused)
    FAILED    = "failed"   # travou/falhou — precisa de atenção humana


#: Status terminais — liberam o slot para a próxima task.
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.DONE,
    RunStatus.REFUSED,
    RunStatus.FAILED,
})


@dataclass(frozen=True, slots=True)
class Run:
    """Uma unidade de trabalho rastreada pelo ledger.

    ``task_key``       — chave canônica da issue pai ("owner/repo#42" ou "VGAT-123")
    ``repo``           — repo resolvido do título
    ``current_stage``  — valor do State atual (ex: "flow:planning-specs")
    ``stage_session``  — id/handle da sessão one-shot do estágio atual (se houver)
    ``started_at``     — ISO timestamp de quando a run foi presa (claim)
    ``last_transition``— ISO timestamp da última mudança de estágio
    ``attempts``       — nº de tentativas do estágio atual (para detectar loop)
    ``status``         — RunStatus
    """

    task_key: str
    repo: str
    current_stage: str
    stage_session: str | None
    started_at: str
    last_transition: str
    attempts: int
    status: RunStatus


# ---------------------------------------------------------------------------
# Interface (Protocol)
# ---------------------------------------------------------------------------

@runtime_checkable
class RunLedger(Protocol):
    """Contrato do ledger de runs do single-flow.

    Qualquer backend (SQLite local agora, store global depois) satisfaz esta
    superfície. O motor depende só dela.
    """

    def claim(self, task_key: str, repo: str, stage: str) -> bool:
        """Tenta prender uma task como a run ativa.

        Retorna ``True`` se conseguiu prender (não havia run ativa), ``False``
        se já existe uma run ativa (single-flow permite só uma por vez).
        Idempotente: chamar com a mesma ``task_key`` já ativa retorna ``True``.
        """
        ...

    def active(self) -> Run | None:
        """Retorna a run ativa (status RUNNING), ou ``None`` se o slot está livre."""
        ...

    def get(self, task_key: str) -> Run | None:
        """Retorna a run de uma task específica, qualquer status, ou ``None``."""
        ...

    def advance(self, task_key: str, new_stage: str, stage_session: str | None = None) -> None:
        """Registra a transição da run para um novo estágio.

        Reseta ``attempts`` para 0 e atualiza ``last_transition``.
        """
        ...

    def bump_attempt(self, task_key: str) -> int:
        """Incrementa e retorna o contador de tentativas do estágio atual."""
        ...

    def release(self, task_key: str, status: RunStatus) -> None:
        """Libera o slot, marcando a run com um status terminal.

        Após release, ``active()`` volta a poder retornar ``None`` e uma nova
        task pode ser presa.
        """
        ...

    def all(self) -> list[Run]:
        """Retorna todas as runs conhecidas (para a visão OTL futura)."""
        ...


# ---------------------------------------------------------------------------
# Implementação SQLite local
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_ledger (
    task_key        TEXT PRIMARY KEY,
    repo            TEXT NOT NULL,
    current_stage   TEXT NOT NULL,
    stage_session   TEXT NULL,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_transition TEXT NOT NULL DEFAULT (datetime('now')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'running'
);
"""

_DEFAULT_DATA_DIR = Path.home() / ".kiro" / "crew" / "kirocrew-flow"


def _db_path(squad_id: str, data_dir: Path | None = None) -> Path:
    base = data_dir or _DEFAULT_DATA_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"run_ledger_{squad_id}.db"


def _row_to_run(row: tuple) -> Run:
    return Run(
        task_key=row[0],
        repo=row[1],
        current_stage=row[2],
        stage_session=row[3],
        started_at=row[4],
        last_transition=row[5],
        attempts=row[6],
        status=RunStatus(row[7]),
    )


class SqliteRunLedger:
    """Implementação local do RunLedger em SQLite stdlib.

    Um banco por squad, em ``<data_dir>/run_ledger_<squad_id>.db``. Segue o
    mesmo padrão zero-dependência de ``flow/scan/cache.py``.
    """

    def __init__(self, squad_id: str, data_dir: Path | None = None) -> None:
        self._conn = sqlite3.connect(str(_db_path(squad_id, data_dir)))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    # -- escrita -----------------------------------------------------------

    def claim(self, task_key: str, repo: str, stage: str) -> bool:
        existing_active = self.active()
        if existing_active is not None:
            # Já é a mesma task — idempotente
            return existing_active.task_key == task_key
        self._conn.execute(
            """
            INSERT INTO run_ledger (task_key, repo, current_stage, status)
            VALUES (?, ?, ?, 'running')
            ON CONFLICT(task_key) DO UPDATE SET
                repo          = excluded.repo,
                current_stage = excluded.current_stage,
                status        = 'running',
                last_transition = datetime('now')
            """,
            (task_key, repo, stage),
        )
        self._conn.commit()
        return True

    def advance(self, task_key: str, new_stage: str, stage_session: str | None = None) -> None:
        self._conn.execute(
            """
            UPDATE run_ledger
               SET current_stage   = ?,
                   stage_session   = ?,
                   attempts        = 0,
                   last_transition = datetime('now')
             WHERE task_key = ?
            """,
            (new_stage, stage_session, task_key),
        )
        self._conn.commit()

    def bump_attempt(self, task_key: str) -> int:
        self._conn.execute(
            "UPDATE run_ledger SET attempts = attempts + 1 WHERE task_key = ?",
            (task_key,),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT attempts FROM run_ledger WHERE task_key = ?", (task_key,)
        ).fetchone()
        return row[0] if row else 0

    def release(self, task_key: str, status: RunStatus) -> None:
        self._conn.execute(
            """
            UPDATE run_ledger
               SET status = ?, last_transition = datetime('now')
             WHERE task_key = ?
            """,
            (status.value, task_key),
        )
        self._conn.commit()

    # -- leitura -----------------------------------------------------------

    def active(self) -> Run | None:
        row = self._conn.execute(
            "SELECT * FROM run_ledger WHERE status = 'running' LIMIT 1"
        ).fetchone()
        return _row_to_run(row) if row else None

    def get(self, task_key: str) -> Run | None:
        row = self._conn.execute(
            "SELECT * FROM run_ledger WHERE task_key = ?", (task_key,)
        ).fetchone()
        return _row_to_run(row) if row else None

    def all(self) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM run_ledger ORDER BY last_transition DESC"
        ).fetchall()
        return [_row_to_run(r) for r in rows]

    # -- ciclo de vida -----------------------------------------------------

    def close(self) -> None:
        self._conn.close()
