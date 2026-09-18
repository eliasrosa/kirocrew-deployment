"""Cache persistente do scan — SQLite zero-dependência.

Armazena o hash das labels de cada issue no último ciclo. Se o hash não
mudou, a issue é ignorada — zero token gasto.

O banco é por squad: ``<data_dir>/scan_cache_<squad_id>.db``
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_cache (
    key         TEXT PRIMARY KEY,
    labels_hash TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DEFAULT_DATA_DIR = Path.home() / ".kiro" / "crew" / "kirocrew-flow"


def _db_path(squad_id: str, data_dir: Path | None = None) -> Path:
    base = data_dir or _DEFAULT_DATA_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"scan_cache_{squad_id}.db"


def open_cache(squad_id: str, data_dir: Path | None = None) -> sqlite3.Connection:
    """Abre (ou cria) o banco de cache da squad."""
    path = _db_path(squad_id, data_dir)
    conn = sqlite3.connect(str(path))
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def get_hash(conn: sqlite3.Connection, key: str) -> str | None:
    """Retorna o hash armazenado para a issue, ou None se não existe."""
    row = conn.execute(
        "SELECT labels_hash FROM issue_cache WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def set_hash(conn: sqlite3.Connection, key: str, labels_hash: str) -> None:
    """Atualiza (ou insere) o hash da issue."""
    conn.execute(
        """
        INSERT INTO issue_cache (key, labels_hash)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET
            labels_hash = excluded.labels_hash,
            updated_at  = datetime('now')
        """,
        (key, labels_hash),
    )
    conn.commit()


def delete_key(conn: sqlite3.Connection, key: str) -> None:
    """Remove uma issue do cache (ex: quando concluída)."""
    conn.execute("DELETE FROM issue_cache WHERE key = ?", (key,))
    conn.commit()


def compute_hash(labels: list[str]) -> str:
    """Hash determinístico de um conjunto de labels (sort antes do hash).

    A ordem das labels não importa — dois conjuntos iguais produzem o
    mesmo hash independente de como vieram da API.
    """
    canonical = ",".join(sorted(labels))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def prune_done(conn: sqlite3.Connection, active_keys: set[str]) -> int:
    """Remove do cache issues que não existem mais (concluídas ou deletadas).

    Retorna o número de linhas removidas.
    """
    if not active_keys:
        return 0
    # Deleta tudo que não está na lista de ativos
    placeholders = ",".join("?" * len(active_keys))
    cursor = conn.execute(
        f"DELETE FROM issue_cache WHERE key NOT IN ({placeholders})",
        list(active_keys),
    )
    conn.commit()
    return cursor.rowcount
