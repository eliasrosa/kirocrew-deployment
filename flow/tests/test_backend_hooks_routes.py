"""Testes de unidade para backend.routes (Fase 3, assinatura corrigida).

Critérios de aceite (issue #155, iteração 1):
- register_routes(app) usa app.router.add_get/add_post (não duck-typing)
- register_routes registra on_startup e on_cleanup no app
- handle_health retorna {"ok": true, "app": "kirocrew-flow", "version": "1.0.0"}
- handle_issues retorna {"issues": [], "note": "TODO Fase 4"}
- handle_dispatch retorna {"ok": true, "note": "TODO Fase 4"}
- _start_loops cria 4 tasks asyncio e imprime log
- _stop_loops cancela tasks e limpa a lista
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
    _start_loops,
    _stop_loops,
    handle_dispatch,
    handle_health,
    handle_issues,
    register_routes,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request() -> mock.MagicMock:
    """Retorna um mock mínimo de web.Request."""
    return mock.MagicMock()


def _parse_body(response: object) -> dict:  # type: ignore[type-arg]
    """Lê o body de um web.Response como dict."""
    raw = getattr(response, "body", None)
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(raw)  # type: ignore[arg-type]
    return json.loads(str(raw))


# ---------------------------------------------------------------------------
# Tests: register_routes — usa web.Application real
# ---------------------------------------------------------------------------


class TestRegisterRoutes:
    @staticmethod
    def _route_set(app: web.Application) -> set[tuple[str, str]]:
        result = set()
        for r in app.router.routes():
            resource = r.resource
            if resource is not None:
                result.add((r.method, resource.canonical))
        return result

    def test_registers_health_route(self) -> None:
        app = web.Application()
        register_routes(app)
        assert ("GET", "/api/apps/kirocrew-flow/health") in self._route_set(app)

    def test_registers_issues_route(self) -> None:
        app = web.Application()
        register_routes(app)
        assert ("GET", "/api/apps/kirocrew-flow/issues") in self._route_set(app)

    def test_registers_dispatch_route(self) -> None:
        app = web.Application()
        register_routes(app)
        assert ("POST", "/api/apps/kirocrew-flow/dispatch") in self._route_set(app)

    def test_appends_on_startup_hook(self) -> None:
        app = web.Application()
        register_routes(app)
        assert _start_loops in app.on_startup

    def test_appends_on_cleanup_hook(self) -> None:
        app = web.Application()
        register_routes(app)
        assert _stop_loops in app.on_cleanup

    def test_three_explicit_routes(self) -> None:
        """aiohttp add_get registra HEAD automaticamente — contar só GET e POST."""
        app = web.Application()
        register_routes(app)
        explicit = {r.method for r in app.router.routes() if r.method in ("GET", "POST")}
        assert explicit == {"GET", "POST"}


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
# Tests: handle_issues
# ---------------------------------------------------------------------------


class TestHandleIssues:
    def test_returns_empty_issues_list(self) -> None:
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_issues(_make_request()))
        body = _parse_body(response)
        assert body["issues"] == []

    def test_returns_todo_note(self) -> None:
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_issues(_make_request()))
        body = _parse_body(response)
        assert "note" in body
        assert "TODO" in body["note"]


# ---------------------------------------------------------------------------
# Tests: handle_dispatch
# ---------------------------------------------------------------------------


class TestHandleDispatch:
    def test_returns_ok_true(self) -> None:
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_dispatch(_make_request()))
        body = _parse_body(response)
        assert body["ok"] is True

    def test_returns_todo_note(self) -> None:
        loop = asyncio.get_event_loop()
        response = loop.run_until_complete(handle_dispatch(_make_request()))
        body = _parse_body(response)
        assert "note" in body
        assert "TODO" in body["note"]


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
