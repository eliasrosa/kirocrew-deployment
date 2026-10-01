"""Testes unitários para os handlers de gate do workflow engine.

Cobre:
- GET  /gate/{token}        → handle_gate_get  + _gate_get
- POST /gate/{token}/decide → handle_gate_decide + _gate_decide

Estratégia: patch de _conn() para usar SQLite in-memory real.
Evita importar yoyo (dependência de runtime, não de testes) e qualquer I/O
de disco. A lógica de negócio é testada end-to-end contra SQL real.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Importa só as funções de routes que usamos -- sem importar backend.engine.db
# (que puxa yoyo). As funções _gate_get/_gate_decide importam db dentro delas,
# então patchamos _conn lá dentro.
from backend.routes import (  # noqa: E402
    _gate_decide,
    _gate_get,
    handle_gate_decide,
    handle_gate_get,
)

# ---------------------------------------------------------------------------
# DB in-memory: schema + fixture
# ---------------------------------------------------------------------------

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS workflow_gate_tokens (
        token        TEXT PRIMARY KEY,
        run_id       TEXT NOT NULL,
        node_id      TEXT NOT NULL,
        prompt       TEXT NOT NULL,
        options_json TEXT NOT NULL DEFAULT '["approve","reject"]',
        decision     TEXT,
        decided_at   TEXT,
        expires_at   TEXT NOT NULL,
        created_at   TEXT NOT NULL
    );
"""


def _make_db(check_same_thread: bool = True) -> sqlite3.Connection:
    """Abre um SQLite in-memory com o schema mínimo do engine.

    check_same_thread=False necessário para os handlers HTTP que usam
    run_in_executor (o SQL roda num thread diferente do que criou a conn).
    """
    conn = sqlite3.connect(":memory:", check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _insert_token(
    conn: sqlite3.Connection,
    token: str,
    run_id: str = "run-001",
    node_id: str = "approve_deploy",
    prompt: str = "Aprovar deploy?",
    options: list[str] | None = None,
    decision: str | None = None,
    expires_delta: timedelta = timedelta(hours=1),
) -> None:
    if options is None:
        options = ["approve", "reject"]
    now = datetime.now(UTC)
    expires_at = (now + expires_delta).isoformat()
    decided_at = now.isoformat() if decision else None
    conn.execute(
        """INSERT INTO workflow_gate_tokens
           (token, run_id, node_id, prompt, options_json,
            decision, decided_at, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (token, run_id, node_id, prompt, json.dumps(options),
         decision, decided_at, expires_at, now.isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers HTTP
# ---------------------------------------------------------------------------

def _make_request(
    token: str = "tok-abc",
    body: dict | None = None,
    bad_json: bool = False,
) -> mock.MagicMock:
    req = mock.MagicMock()
    req.match_info = {"token": token}
    if bad_json:
        async def _bad() -> dict:
            raise ValueError("invalid json")
        req.json = _bad
    elif body is not None:
        async def _ok() -> dict:
            return body
        req.json = _ok
    return req


def _body(response: object) -> dict:
    raw = getattr(response, "body", None)
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(raw)
    return json.loads(str(raw))


def _status(response: object) -> int:
    return getattr(response, "status", 200)


# ---------------------------------------------------------------------------
# Patch helper mais simples: substituir sys.modules["backend.engine.db"]
# ---------------------------------------------------------------------------

@contextmanager
def _with_db(conn: sqlite3.Connection):
    """Injeta um módulo fake de backend.engine.db que usa conn in-memory."""
    import types

    mod = types.ModuleType("backend.engine.db")

    @contextmanager
    def _conn_cm():
        yield conn

    mod._conn = _conn_cm  # type: ignore[attr-defined]

    def decide_gate_token(token: str, decision: str) -> bool:
        cur = conn.execute(
            "UPDATE workflow_gate_tokens SET decision=?, decided_at=? "
            "WHERE token=? AND decision IS NULL",
            (decision, datetime.now(UTC).isoformat(), token),
        )
        conn.commit()
        return cur.rowcount > 0

    mod.decide_gate_token = decide_gate_token  # type: ignore[attr-defined]

    old = sys.modules.get("backend.engine.db")
    sys.modules["backend.engine.db"] = mod
    try:
        yield mod
    finally:
        if old is None:
            sys.modules.pop("backend.engine.db", None)
        else:
            sys.modules["backend.engine.db"] = old


# ---------------------------------------------------------------------------
# _gate_get
# ---------------------------------------------------------------------------

class TestGateGet:

    def test_token_nao_encontrado(self):
        conn = _make_db()
        with _with_db(conn):
            result = _gate_get("token-inexistente")
        assert result is None

    def test_token_pendente(self):
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token, prompt="Publicar em PRD?")
        with _with_db(conn):
            result = _gate_get(token)
        assert result is not None
        assert result["token"] == token
        assert result["decision"] is None
        assert result["prompt"] == "Publicar em PRD?"
        assert result["options"] == ["approve", "reject"]

    def test_token_decidido(self):
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token, decision="approve")
        with _with_db(conn):
            result = _gate_get(token)
        assert result is not None
        assert result["decision"] == "approve"

    def test_options_json_deserializado(self):
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token, options=["yes", "no", "skip"])
        with _with_db(conn):
            result = _gate_get(token)
        assert result is not None
        assert result["options"] == ["yes", "no", "skip"]
        assert "options_json" not in result


# ---------------------------------------------------------------------------
# _gate_decide
# ---------------------------------------------------------------------------

class TestGateDecide:

    def test_token_nao_encontrado(self):
        conn = _make_db()
        with _with_db(conn):
            result = _gate_decide("tok-x", "approve")
        assert result["ok"] is False
        assert result["error"] == "not_found"

    def test_token_expirado(self):
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token, expires_delta=timedelta(seconds=-1))
        with _with_db(conn):
            result = _gate_decide(token, "approve")
        assert result["ok"] is False
        assert result["error"] == "token_expired"

    def test_decisao_invalida(self):
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token, options=["approve", "reject"])
        with _with_db(conn):
            result = _gate_decide(token, "maybe")
        assert result["ok"] is False
        assert result["error"] == "invalid_option"
        assert set(result["valid_options"]) == {"approve", "reject"}

    def test_decisao_registrada(self):
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token)
        with _with_db(conn):
            result = _gate_decide(token, "approve")
        assert result["ok"] is True
        assert result["decision"] == "approve"
        assert result.get("idempotent") is not True

    def test_idempotente_mesma_decisao(self):
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token, decision="approve")
        with _with_db(conn):
            result = _gate_decide(token, "approve")
        assert result["ok"] is True
        assert result["idempotent"] is True

    def test_conflito_decisao_diferente(self):
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token, decision="approve")
        with _with_db(conn):
            result = _gate_decide(token, "reject")
        assert result["ok"] is False
        assert result["error"] == "already_decided"
        assert result["decision"] == "approve"

    def test_decisao_persiste_no_db(self):
        """Verifica que a decisão foi de fato gravada."""
        conn = _make_db()
        token = str(uuid.uuid4())
        _insert_token(conn, token)
        with _with_db(conn):
            _gate_decide(token, "reject")
            result = _gate_get(token)
        assert result is not None
        assert result["decision"] == "reject"
        assert result["decided_at"] is not None


# ---------------------------------------------------------------------------
# handle_gate_get (handler HTTP)
# ---------------------------------------------------------------------------

class TestHandleGateGet:

    def test_token_ausente_retorna_400(self):
        req = mock.MagicMock()
        req.match_info = {"token": ""}
        conn = _make_db(check_same_thread=False)
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_get(req))
        assert _status(resp) == 400

    def test_token_nao_encontrado_retorna_404(self):
        req = _make_request(token="tok-x")
        conn = _make_db(check_same_thread=False)
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_get(req))
        assert _status(resp) == 404

    def test_token_encontrado_retorna_200(self):
        conn = _make_db(check_same_thread=False)
        token = str(uuid.uuid4())
        _insert_token(conn, token, prompt="Publicar em PRD?")
        req = _make_request(token=token)
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_get(req))
        assert _status(resp) == 200
        data = _body(resp)
        assert data["token"] == token
        assert data["prompt"] == "Publicar em PRD?"
        assert isinstance(data["options"], list)


# ---------------------------------------------------------------------------
# handle_gate_decide (handler HTTP)
# ---------------------------------------------------------------------------

class TestHandleGateDecide:

    def test_token_ausente_retorna_400(self):
        req = mock.MagicMock()
        req.match_info = {"token": ""}
        conn = _make_db(check_same_thread=False)
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_decide(req))
        assert _status(resp) == 400

    def test_body_json_invalido_retorna_400(self):
        req = _make_request(token="tok-x", bad_json=True)
        conn = _make_db(check_same_thread=False)
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_decide(req))
        assert _status(resp) == 400

    def test_decision_ausente_retorna_400(self):
        conn = _make_db(check_same_thread=False)
        token = str(uuid.uuid4())
        _insert_token(conn, token)
        req = _make_request(token=token, body={})
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_decide(req))
        assert _status(resp) == 400

    def test_decisao_valida_retorna_200(self):
        conn = _make_db(check_same_thread=False)
        token = str(uuid.uuid4())
        _insert_token(conn, token)
        req = _make_request(token=token, body={"decision": "approve"})
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_decide(req))
        assert _status(resp) == 200
        data = _body(resp)
        assert data["ok"] is True
        assert data["decision"] == "approve"

    def test_token_expirado_retorna_409(self):
        conn = _make_db(check_same_thread=False)
        token = str(uuid.uuid4())
        _insert_token(conn, token, expires_delta=timedelta(seconds=-1))
        req = _make_request(token=token, body={"decision": "approve"})
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_decide(req))
        assert _status(resp) == 409
        assert _body(resp)["error"] == "token_expired"

    def test_idempotente_retorna_200(self):
        conn = _make_db(check_same_thread=False)
        token = str(uuid.uuid4())
        _insert_token(conn, token, decision="approve")
        req = _make_request(token=token, body={"decision": "approve"})
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_decide(req))
        assert _status(resp) == 200
        assert _body(resp)["idempotent"] is True

    def test_conflito_retorna_409(self):
        conn = _make_db(check_same_thread=False)
        token = str(uuid.uuid4())
        _insert_token(conn, token, decision="approve")
        req = _make_request(token=token, body={"decision": "reject"})
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_decide(req))
        assert _status(resp) == 409
        assert _body(resp)["error"] == "already_decided"

    def test_opcao_invalida_retorna_409(self):
        conn = _make_db(check_same_thread=False)
        token = str(uuid.uuid4())
        _insert_token(conn, token, options=["approve", "reject"])
        req = _make_request(token=token, body={"decision": "maybe"})
        with _with_db(conn):
            resp = asyncio.get_event_loop().run_until_complete(handle_gate_decide(req))
        assert _status(resp) == 409
        assert _body(resp)["error"] == "invalid_option"
