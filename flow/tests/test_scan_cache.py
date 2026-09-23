"""Testes de flow/scan/cache.py — todos sem rede, sem mock."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from flow.scan.cache import (
    compute_hash,
    delete_key,
    get_hash,
    open_cache,
    prune_done,
    set_hash,
)


@pytest.fixture
def tmp_conn() -> sqlite3.Connection:
    """Conexão em memória para testes rápidos."""
    conn = sqlite3.connect(":memory:")
    from flow.scan.cache import _SCHEMA
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


class TestComputeHash:
    def test_hash_deterministico(self) -> None:
        h1 = compute_hash(["flow:develop-waiting", "phase-1"])
        h2 = compute_hash(["phase-1", "flow:develop-waiting"])
        assert h1 == h2

    def test_hash_difere_para_labels_diferentes(self) -> None:
        assert compute_hash(["flow:develop-waiting"]) != compute_hash(["flow:develop-running"])

    def test_hash_lista_vazia(self) -> None:
        h = compute_hash([])
        assert isinstance(h, str) and len(h) == 16

    def test_hash_tem_16_chars(self) -> None:
        assert len(compute_hash(["flow:develop-waiting"])) == 16


class TestCacheOperations:
    def test_get_hash_retorna_none_para_chave_nova(self, tmp_conn: sqlite3.Connection) -> None:
        assert get_hash(tmp_conn, "VGAT-999") is None

    def test_set_e_get_hash(self, tmp_conn: sqlite3.Connection) -> None:
        set_hash(tmp_conn, "VGAT-1", "abc123")
        assert get_hash(tmp_conn, "VGAT-1") == "abc123"

    def test_set_hash_atualiza_existente(self, tmp_conn: sqlite3.Connection) -> None:
        set_hash(tmp_conn, "VGAT-1", "hash_antigo")
        set_hash(tmp_conn, "VGAT-1", "hash_novo")
        assert get_hash(tmp_conn, "VGAT-1") == "hash_novo"

    def test_delete_key_remove_entrada(self, tmp_conn: sqlite3.Connection) -> None:
        set_hash(tmp_conn, "VGAT-1", "abc")
        delete_key(tmp_conn, "VGAT-1")
        assert get_hash(tmp_conn, "VGAT-1") is None

    def test_delete_key_nao_falha_se_nao_existe(self, tmp_conn: sqlite3.Connection) -> None:
        delete_key(tmp_conn, "VGAT-INEXISTENTE")  # não deve lançar


class TestPruneDone:
    def test_remove_issues_inativas(self, tmp_conn: sqlite3.Connection) -> None:
        set_hash(tmp_conn, "VGAT-1", "h1")
        set_hash(tmp_conn, "VGAT-2", "h2")
        set_hash(tmp_conn, "VGAT-3", "h3")

        removed = prune_done(tmp_conn, active_keys={"VGAT-1", "VGAT-2"})

        assert removed == 1
        assert get_hash(tmp_conn, "VGAT-3") is None
        assert get_hash(tmp_conn, "VGAT-1") is not None

    def test_prune_com_active_vazio_nao_deleta_nada(self, tmp_conn: sqlite3.Connection) -> None:
        set_hash(tmp_conn, "VGAT-1", "h1")
        removed = prune_done(tmp_conn, active_keys=set())
        assert removed == 0

    def test_prune_retorna_zero_quando_nada_para_remover(self, tmp_conn: sqlite3.Connection) -> None:
        set_hash(tmp_conn, "VGAT-1", "h1")
        removed = prune_done(tmp_conn, active_keys={"VGAT-1"})
        assert removed == 0


class TestOpenCache:
    def test_cria_banco_em_disco(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_cache("test-squad", data_dir=Path(tmp))
            assert conn is not None
            # Verifica que a tabela foi criada
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert ("issue_cache",) in tables
            conn.close()

    def test_reabrindo_mantem_dados(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn1 = open_cache("test-squad", data_dir=Path(tmp))
            set_hash(conn1, "VGAT-1", "persistido")
            conn1.close()

            conn2 = open_cache("test-squad", data_dir=Path(tmp))
            assert get_hash(conn2, "VGAT-1") == "persistido"
            conn2.close()
