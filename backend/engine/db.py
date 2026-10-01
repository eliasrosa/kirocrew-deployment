"""
backend/engine/db.py
Acesso ao SQLite do workflow engine.
Aplica migrações yoyo no startup e expõe helpers CRUD.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# yoyo-migrations: pip install yoyo-migrations
from yoyo import get_backend, read_migrations

# DB fica junto dos outros SQLites do kirocrew-flow
_APP_DATA = Path.home() / ".kiro/crew/kirocrew-flow"
DB_PATH = _APP_DATA / "workflow_engine.db"
MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


# ── Migrações ──────────────────────────────────────────────────────────────

def apply_migrations() -> None:
    """Aplica migrações pendentes. Chamar uma vez no on_startup."""
    _APP_DATA.mkdir(parents=True, exist_ok=True)
    backend = get_backend(f"sqlite:///{DB_PATH}")
    migrations = read_migrations(str(MIGRATIONS_DIR))
    with backend.lock():
        backend.apply_migrations(backend.to_apply(migrations))


# ── Conexão ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


# ── workflow_runs ──────────────────────────────────────────────────────────

def create_run(run_id: str, workflow_id: str, first_node: str, node_ttl_secs: int = 3600) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO workflow_runs
                (id, workflow_id, status, current_node, node_ttl_secs, started_at, updated_at)
            VALUES (?, ?, 'running', ?, ?, ?, ?)
            """,
            (run_id, workflow_id, first_node, node_ttl_secs, _now(), _now()),
        )


def get_run(run_id: str) -> dict[str, Any] | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return dict(row) if row else None


def list_active_runs(run_id: str | None = None) -> list[dict[str, Any]]:
    """Retorna runs com status=running. Filtra por run_id se fornecido."""
    sql = "SELECT * FROM workflow_runs WHERE status = 'running'"
    params: list[str] = []
    if run_id:
        sql += " AND id = ?"
        params.append(run_id)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def advance_run(run_id: str, next_node: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE workflow_runs SET current_node = ?, updated_at = ? WHERE id = ?",
            (next_node, _now(), run_id),
        )


def complete_run(run_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            UPDATE workflow_runs
            SET status = 'completed', finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (_now(), _now(), run_id),
        )


def fail_run(run_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            UPDATE workflow_runs
            SET status = 'failed', finished_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (_now(), _now(), run_id),
        )


def set_run_session_key(run_id: str, session_key: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE workflow_runs SET session_key = ?, updated_at = ? WHERE id = ?",
            (session_key, _now(), run_id),
        )


# ── workflow_node_states ───────────────────────────────────────────────────

def start_node(run_id: str, node_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO workflow_node_states (run_id, node_id, status, started_at)
            VALUES (?, ?, 'running', ?)
            ON CONFLICT (run_id, node_id) DO UPDATE SET
                status = 'running', started_at = excluded.started_at
            """,
            (run_id, node_id, _now()),
        )


def complete_node(
    run_id: str,
    node_id: str,
    exit_status: str,
    output: dict[str, Any],
    exit_code: int | None = None,
    error_kind: str | None = None,   # gap #2: failed | timed_out | error
) -> None:
    with _conn() as conn:
        conn.execute(
            """
            UPDATE workflow_node_states
            SET status = 'completed',
                exit_status = ?,
                output_json = ?,
                exit_code = ?,
                error_kind = ?,
                finished_at = ?
            WHERE run_id = ? AND node_id = ?
            """,
            (exit_status, json.dumps(output), exit_code, error_kind, _now(), run_id, node_id),
        )


def get_node_output(run_id: str, node_id: str) -> dict[str, Any]:
    """Retorna o output_json de um nó concluído, ou {} se não existir."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT output_json FROM workflow_node_states WHERE run_id = ? AND node_id = ?",
            (run_id, node_id),
        ).fetchone()
    if not row or not row["output_json"]:
        return {}
    return json.loads(row["output_json"])


def get_first_output_field(run_id: str, field: str) -> Any:
    """Resolve $nodes.*.output.<field>: primeiro valor não-nulo entre nós concluídos."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT output_json FROM workflow_node_states
            WHERE run_id = ? AND status = 'completed' AND output_json IS NOT NULL
            ORDER BY finished_at ASC
            """,
            (run_id,),
        ).fetchall()
    for row in rows:
        output = json.loads(row["output_json"])
        if field in output and output[field] is not None:
            return output[field]
    return None


# ── workflow_gate_tokens (gap #3: aprovação humana) ───────────────────────

def create_gate_token(
    token: str,
    run_id: str,
    node_id: str,
    prompt: str,
    options: list[str],
    ttl_secs: int,
) -> None:
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_secs)).isoformat()
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO workflow_gate_tokens
                (token, run_id, node_id, prompt, options_json, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (token, run_id, node_id, prompt, json.dumps(options), expires, _now()),
        )


def get_gate_token(run_id: str, node_id: str) -> dict[str, Any] | None:
    """Retorna o token mais recente para run+nó, ou None se não existe."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM workflow_gate_tokens
            WHERE run_id = ? AND node_id = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (run_id, node_id),
        ).fetchone()
    return dict(row) if row else None


def decide_gate_token(token: str, decision: str) -> bool:
    """Registra a decisão humana. Retorna True se o token existia e estava pendente."""
    with _conn() as conn:
        cur = conn.execute(
            """
            UPDATE workflow_gate_tokens
            SET decision = ?, decided_at = ?
            WHERE token = ? AND decision IS NULL
            """,
            (decision, _now(), token),
        )
    return cur.rowcount > 0
