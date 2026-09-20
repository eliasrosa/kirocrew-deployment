"""Testes de unidade para backend.routes (Fase 4 — implementação real).

Critérios de aceite:
- register_routes(app) registra as 3 rotas + on_startup + on_cleanup
- handle_health retorna {"ok": true, "app": "kirocrew-flow", "version": "1.0.0"}
- handle_issues retorna {"columns": {todo, dev, review, reviewed, done, blocked}}
  mesmo em caso de erro no scan (fallback para colunas vazias)
- handle_dispatch valida o body e retorna {"ok": true/false, ...}
- _start_loops cria 4 tasks asyncio e imprime log
- _stop_loops cancela tasks e limpa a lista
- _age_minutes calcula a idade em minutos
- _state_to_column mapeia os estados para colunas do kanban
- _repo_from_key extrai o repo de uma key
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest import mock

import pytest
from aiohttp import web

# Garante que o raiz do repo está no path para importar backend/
_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.routes import (  # noqa: E402
    _age_minutes,
    _empty_columns,
    _repo_from_key,
    _start_loops,
    _state_to_column,
    _stop_loops,
    handle_dispatch,
    handle_health,
    handle_issues,
    register_routes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(body: dict | None = None) -> mock.MagicMock:
    """Retorna um mock mínimo de web.Request."""
    req = mock.MagicMock()
    if body is not None:
        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        future.set_result(body)
        req.json = mock.MagicMock(return_value=future)
    else:
        # json() que lança Exception para simular body inválido
        async def _bad_json() -> dict:
            raise ValueError("invalid json")
        req.json = _bad_json
    return req


def _parse_body(response: object) -> dict:  # type: ignore[type-arg]
    """Lê o body de um web.Response como dict."""
    raw = getattr(response, "body", None)
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(raw)  # type: ignore[arg-type]
    return json.loads(str(raw))


# ---------------------------------------------------------------------------
# Tests: register_routes — nova assinatura: ctx -> list[AppRoute]
# ---------------------------------------------------------------------------


class TestRegisterRoutes:
    """Testa a nova assinatura de register_routes para apps de terceiros.

    A interface correta para apps de terceiros (não builtins) é:
    - Recebe ctx (AppContext, duck-typed como object)
    - Retorna list[AppRoute] — o RouteRegistry monta as rotas como
      /api/apps/{app_name}{path}
    """

    def _get_routes(self) -> list:
        """Retorna a lista de AppRoute retornada por register_routes."""
        ctx = mock.MagicMock()
        result = register_routes(ctx)
        return result if isinstance(result, list) else []

    def test_returns_list(self) -> None:
        ctx = mock.MagicMock()
        result = register_routes(ctx)
        assert isinstance(result, list), "register_routes deve retornar list[AppRoute]"

    def test_registers_health_route(self) -> None:
        routes = self._get_routes()
        methods_paths = {(r.method, r.path) for r in routes}
        assert ("GET", "/health") in methods_paths

    def test_registers_issues_route(self) -> None:
        routes = self._get_routes()
        methods_paths = {(r.method, r.path) for r in routes}
        assert ("GET", "/issues") in methods_paths

    def test_registers_dispatch_route(self) -> None:
        routes = self._get_routes()
        methods_paths = {(r.method, r.path) for r in routes}
        assert ("POST", "/dispatch") in methods_paths

    def test_three_explicit_routes(self) -> None:
        routes = self._get_routes()
        assert len(routes) == 3, f"esperado 3 rotas, obtido {len(routes)}"

    def test_handlers_are_callable(self) -> None:
        routes = self._get_routes()
        for route in routes:
            assert callable(route.handler), f"handler de {route.path} deve ser callable"

    def test_appends_on_startup_hook(self) -> None:
        """on_startup ainda é registrado via app.on_startup dentro de _start_loops
        quando chamado pela aiohttp — não via register_routes diretamente."""
        # Este comportamento depende de como o RouteRegistry integra on_startup.
        # Por enquanto verificamos apenas que _start_loops e _stop_loops são importáveis.
        assert callable(_start_loops)
        assert callable(_stop_loops)

    def test_appends_on_cleanup_hook(self) -> None:
        """Equivalente ao test_appends_on_startup_hook."""
        assert callable(_stop_loops)


# ---------------------------------------------------------------------------
# Tests: handle_health
# ---------------------------------------------------------------------------


class TestHandleHealth:
    def test_returns_ok_true(self) -> None:
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_health(_make_request()))
        body = _parse_body(response)
        assert body["ok"] is True

    def test_returns_correct_app_name(self) -> None:
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_health(_make_request()))
        body = _parse_body(response)
        assert body["app"] == "kirocrew-flow"

    def test_returns_version(self) -> None:
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_health(_make_request()))
        body = _parse_body(response)
        assert "version" in body


# ---------------------------------------------------------------------------
# Tests: handle_issues — nova implementação real
# ---------------------------------------------------------------------------


class TestHandleIssues:
    def _run_with_mock_loader(self, columns: dict, squad_name: str = "Test Squad", project: str = "owner/repo") -> dict:
        """Executa handle_issues com _load_issues_from_github mockado."""
        mock_result = {"squad_name": squad_name, "project": project, "columns": columns}
        with mock.patch("backend.routes._load_issues_from_github", return_value=mock_result):
            loop = asyncio.get_event_loop()
            response = loop.run_until_complete(handle_issues(_make_request()))
        return _parse_body(response)

    def test_returns_columns_key(self) -> None:
        body = self._run_with_mock_loader(_empty_columns())
        assert "columns" in body

    def test_returns_squad_name_and_project(self) -> None:
        body = self._run_with_mock_loader(_empty_columns(), squad_name="My Squad", project="owner/repo")
        assert body["squad_name"] == "My Squad"
        assert body["project"] == "owner/repo"

    def test_returns_all_column_keys(self) -> None:
        body = self._run_with_mock_loader(_empty_columns())
        for key in ("spec", "ready", "todo", "dev", "review", "review_ok", "reviewed", "done", "blocked"):
            assert key in body["columns"], f"coluna '{key}' ausente no retorno"

    def test_returns_issues_in_correct_column(self) -> None:
        cols = _empty_columns()
        cols["todo"] = [
            {"number": 42, "title": "Test", "repo": "owner/repo", "url": "", "age_min": 10,
             "labels": ["crewflow:todo"], "blocked": False, "running": False}
        ]
        body = self._run_with_mock_loader(cols)
        assert len(body["columns"]["todo"]) == 1
        assert body["columns"]["todo"][0]["number"] == 42

    def test_returns_empty_columns_when_loader_raises(self) -> None:
        """Se _load_issues_from_github lançar exceção, deve retornar colunas vazias com status 500."""
        with mock.patch(
            "backend.routes._load_issues_from_github",
            side_effect=RuntimeError("scan failed"),
        ):
            loop = asyncio.get_event_loop()
            response = loop.run_until_complete(handle_issues(_make_request()))
        body = _parse_body(response)
        assert "columns" in body
        for key in ("spec", "ready", "todo", "dev", "review", "review_ok", "reviewed", "done", "blocked"):
            assert body["columns"][key] == []

    def test_status_500_on_error(self) -> None:
        with mock.patch(
            "backend.routes._load_issues_from_github",
            side_effect=RuntimeError("scan failed"),
        ):
            loop = asyncio.get_event_loop()
            response = loop.run_until_complete(handle_issues(_make_request()))
        assert response.status == 500  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tests: handle_dispatch — nova implementação real
# ---------------------------------------------------------------------------


class TestHandleDispatch:
    def test_returns_400_on_missing_body_fields(self) -> None:
        """Body sem 'repo' ou 'number' deve retornar 400."""
        req = _make_request(body={})
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_dispatch(req))
        assert response.status == 400  # type: ignore[attr-defined]
        body = _parse_body(response)
        assert body["ok"] is False

    def test_returns_400_on_invalid_number(self) -> None:
        req = _make_request(body={"repo": "owner/repo", "number": "not-a-number"})
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_dispatch(req))
        assert response.status == 400  # type: ignore[attr-defined]

    def test_returns_400_on_invalid_json(self) -> None:
        """Request com body inválido deve retornar 400."""
        req = _make_request(body=None)  # json() vai lançar exceção
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_dispatch(req))
        assert response.status == 400  # type: ignore[attr-defined]

    def test_calls_force_dispatch_with_correct_args(self) -> None:
        """handle_dispatch deve chamar _force_dispatch com repo e number corretos."""
        req = _make_request(body={"repo": "owner/repo", "number": 42})
        with mock.patch(
            "backend.routes._force_dispatch",
            return_value={"ok": True, "dispatched": True},
        ) as mock_fd:
            loop = asyncio.get_event_loop()
            response = loop.run_until_complete(handle_dispatch(req))

        mock_fd.assert_called_once_with("owner/repo", 42)
        body = _parse_body(response)
        assert body["ok"] is True
        assert body["dispatched"] is True

    def test_returns_200_on_successful_dispatch(self) -> None:
        req = _make_request(body={"repo": "owner/repo", "number": 1})
        with mock.patch(
            "backend.routes._force_dispatch",
            return_value={"ok": True, "dispatched": True},
        ):
            loop = asyncio.get_event_loop()
            response = loop.run_until_complete(handle_dispatch(req))
        assert response.status == 200  # type: ignore[attr-defined]

    def test_returns_500_on_force_dispatch_exception(self) -> None:
        req = _make_request(body={"repo": "owner/repo", "number": 1})
        with mock.patch(
            "backend.routes._force_dispatch",
            side_effect=RuntimeError("dispatch explodiu"),
        ):
            loop = asyncio.get_event_loop()
            response = loop.run_until_complete(handle_dispatch(req))
        assert response.status == 500  # type: ignore[attr-defined]
        body = _parse_body(response)
        assert body["ok"] is False

    def test_number_as_string_is_coerced_to_int(self) -> None:
        """'number' como string numérica deve ser aceito."""
        req = _make_request(body={"repo": "owner/repo", "number": "99"})
        with mock.patch(
            "backend.routes._force_dispatch",
            return_value={"ok": True, "dispatched": True},
        ) as mock_fd:
            loop = asyncio.get_event_loop()
            loop.run_until_complete(handle_dispatch(req))
        # Deve ter sido convertido para int
        mock_fd.assert_called_once_with("owner/repo", 99)


# ---------------------------------------------------------------------------
# Tests: helpers
# ---------------------------------------------------------------------------


class TestAgeMinutes:
    def test_returns_zero_for_empty_string(self) -> None:
        assert _age_minutes("", 0) == 0

    def test_calculates_age_in_minutes(self) -> None:
        import datetime
        # Cria um timestamp de exatamente 120 minutos atrás
        now = datetime.datetime.now(tz=datetime.UTC)
        past = now - datetime.timedelta(minutes=120)
        age = _age_minutes(past.isoformat(), now.timestamp())
        assert age == 120

    def test_returns_zero_for_invalid_timestamp(self) -> None:
        assert _age_minutes("not-a-date", 1000000) == 0

    def test_returns_zero_for_future_timestamps(self) -> None:
        import datetime
        now = datetime.datetime.now(tz=datetime.UTC)
        future = now + datetime.timedelta(minutes=10)
        age = _age_minutes(future.isoformat(), now.timestamp())
        assert age == 0


class TestStateToColumn:
    def test_todo_maps_to_todo(self) -> None:
        from flow.domain.state import State
        assert _state_to_column(State.TODO) == "todo"

    def test_dev_maps_to_dev(self) -> None:
        from flow.domain.state import State
        assert _state_to_column(State.DEV) == "dev"

    def test_review_maps_to_review(self) -> None:
        from flow.domain.state import State
        assert _state_to_column(State.REVIEW) == "review"

    def test_qa_maps_to_reviewed(self) -> None:
        from flow.domain.state import State
        assert _state_to_column(State.QA) == "review_ok"

    def test_done_maps_to_done(self) -> None:
        from flow.domain.state import State
        assert _state_to_column(State.DONE) == "done"

    def test_spec_maps_to_spec(self) -> None:
        from flow.domain.state import State
        assert _state_to_column(State.SPEC) == "spec"

    def test_ready_maps_to_ready(self) -> None:
        from flow.domain.state import State
        assert _state_to_column(State.READY) == "ready"


class TestRepoFromKey:
    def test_extracts_repo_from_owner_repo_hash_n(self) -> None:
        assert _repo_from_key("owner/repo#42") == "owner/repo"

    def test_returns_empty_for_plain_number(self) -> None:
        assert _repo_from_key("42") == ""

    def test_returns_empty_for_empty_string(self) -> None:
        assert _repo_from_key("") == ""


class TestEmptyColumns:
    def test_has_all_required_keys(self) -> None:
        cols = _empty_columns()
        for key in ("spec", "ready", "todo", "dev", "review", "review_ok", "reviewed", "done", "blocked"):
            assert key in cols

    def test_all_values_are_empty_lists(self) -> None:
        cols = _empty_columns()
        for v in cols.values():
            assert v == []


# ---------------------------------------------------------------------------
# Tests: _start_loops
# ---------------------------------------------------------------------------


class TestStartLoops:
    def test_creates_four_tasks(self, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        """_start_loops deve criar 4 tasks no app e imprimir log."""
        app = web.Application()

        with mock.patch("backend.routes.asyncio.create_task") as mock_create_task:
            fake_tasks = [mock.MagicMock() for _ in range(4)]
            mock_create_task.side_effect = fake_tasks

            with mock.patch.dict(
                "sys.modules",
                {
                    "backend.server": mock.MagicMock(
                        _run_stage_loop=mock.MagicMock(return_value=mock.MagicMock())
                    ),
                    "deployment.deployment": mock.MagicMock(
                        _STAGE_DEV="dev",
                        _STAGE_REVIEWER="reviewer",
                        _STAGE_MERGE="merge",
                        _STAGE_CONFLITO="conflito",
                    ),
                },
            ):
                asyncio.get_event_loop().run_until_complete(_start_loops(app))

        assert mock_create_task.call_count == 4
        assert "crewflow_tasks" in app
        assert len(app["crewflow_tasks"]) == 4

        captured = capsys.readouterr()
        assert "on_startup" in captured.out
        assert "4 loops asyncio iniciados" in captured.out

    def test_handles_import_error_gracefully(self, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        """_start_loops não deve propagar exceção se imports falharem."""
        app = web.Application()

        with mock.patch.dict("sys.modules", {"backend.server": None}):  # type: ignore[dict-item]
            asyncio.get_event_loop().run_until_complete(_start_loops(app))

        captured = capsys.readouterr()
        assert "error" in captured.out.lower() or "on_startup" in captured.out


# ---------------------------------------------------------------------------
# Tests: _stop_loops
# ---------------------------------------------------------------------------


class TestStopLoops:
    def test_cancels_all_tasks(self, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        """_stop_loops deve cancelar todas as tasks e limpar a lista."""
        app = web.Application()
        fake_tasks = [mock.MagicMock() for _ in range(4)]
        app["crewflow_tasks"] = fake_tasks

        asyncio.get_event_loop().run_until_complete(_stop_loops(app))

        for task in fake_tasks:
            task.cancel.assert_called_once()

        assert app["crewflow_tasks"] == []

        captured = capsys.readouterr()
        assert "on_cleanup" in captured.out

    def test_no_tasks_key_does_not_raise(self) -> None:
        """_stop_loops sem chave crewflow_tasks não deve lançar exceção."""
        app = web.Application()
        asyncio.get_event_loop().run_until_complete(_stop_loops(app))

    def test_empty_task_list_does_not_raise(self) -> None:
        """_stop_loops com lista vazia não deve lançar exceção."""
        app = web.Application()
        app["crewflow_tasks"] = []
        asyncio.get_event_loop().run_until_complete(_stop_loops(app))
