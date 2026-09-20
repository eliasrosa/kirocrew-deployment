"""Testes do ciclo de vida ctx-based de backend.hooks (issue #170).

O gateway do Kiro Crew invoca os hooks de ciclo de vida como ``func(ctx)``
(um único argumento posicional = AppContext, aguardado por ser coroutine —
ver kiro_crew/apps/lifecycle.py:_invoke). Uma assinatura de zero argumentos
levanta TypeError. Estes testes travam esse contrato:

- register_routes(ctx) retorna exatamente 3 AppRoute com os pares
  (method, path) corretos e handlers callable.
- on_startup/on_shutdown são coroutines que aceitam UM argumento ctx.
- on_startup cria 4 tasks asyncio; on_shutdown cancela-as e limpa o registro.
- on_startup engole ImportError sem propagar.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest import mock

import pytest

# Garante que o raiz do repo está no path para importar backend/
_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend import hooks  # noqa: E402
from backend.hooks import on_shutdown, on_startup  # noqa: E402
from backend.routes import register_routes  # noqa: E402

# ---------------------------------------------------------------------------
# register_routes(ctx) -> list[AppRoute] — contrato fixo (3 rotas)
# ---------------------------------------------------------------------------


class TestRegisterRoutesContract:
    def _routes(self) -> list:
        result = register_routes(mock.MagicMock())
        assert isinstance(result, list)
        return result

    def test_returns_exactly_three_routes(self) -> None:
        assert len(self._routes()) == 3

    def test_exact_method_path_pairs(self) -> None:
        pairs = {(r.method, r.path) for r in self._routes()}
        assert pairs == {
            ("GET", "/health"),
            ("GET", "/issues"),
            ("POST", "/dispatch"),
        }

    def test_handlers_are_callable(self) -> None:
        for route in self._routes():
            assert callable(route.handler)


# ---------------------------------------------------------------------------
# on_startup / on_shutdown — assinatura single-ctx e coroutine
# ---------------------------------------------------------------------------


class TestHookSignatures:
    def test_on_startup_is_coroutine_function(self) -> None:
        assert inspect.iscoroutinefunction(on_startup)

    def test_on_shutdown_is_coroutine_function(self) -> None:
        assert inspect.iscoroutinefunction(on_shutdown)

    def test_on_startup_accepts_single_positional_ctx(self) -> None:
        sig = inspect.signature(on_startup)
        assert len(sig.parameters) == 1

    def test_on_shutdown_accepts_single_positional_ctx(self) -> None:
        sig = inspect.signature(on_shutdown)
        assert len(sig.parameters) == 1

    def test_calling_on_startup_with_one_arg_does_not_typeerror(self) -> None:
        """func(ctx) — como o gateway invoca — não pode levantar TypeError."""
        hooks._TASKS.clear()
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
        ), mock.patch("backend.hooks.asyncio.create_task") as mock_create_task:
            mock_create_task.side_effect = [mock.MagicMock() for _ in range(4)]
            # ctx posicional único; não deve levantar
            asyncio.get_event_loop().run_until_complete(on_startup(mock.MagicMock()))
        hooks._TASKS.clear()

    def test_calling_on_shutdown_with_one_arg_does_not_typeerror(self) -> None:
        hooks._TASKS.clear()
        asyncio.get_event_loop().run_until_complete(on_shutdown(mock.MagicMock()))


# ---------------------------------------------------------------------------
# on_startup cria 4 tasks; on_shutdown cancela e limpa
# ---------------------------------------------------------------------------


class TestLoopLifecycle:
    def test_on_startup_creates_four_tasks(
        self, capsys: pytest.CaptureFixture
    ) -> None:  # type: ignore[type-arg]
        hooks._TASKS.clear()
        fake_tasks = [mock.MagicMock() for _ in range(4)]
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
        ), mock.patch("backend.hooks.asyncio.create_task") as mock_create_task:
            mock_create_task.side_effect = fake_tasks
            asyncio.get_event_loop().run_until_complete(on_startup(mock.MagicMock()))

        assert mock_create_task.call_count == 4
        assert len(hooks._TASKS) == 4
        captured = capsys.readouterr()
        assert "on_startup" in captured.out
        assert "4 loops asyncio iniciados" in captured.out
        hooks._TASKS.clear()

    def test_on_shutdown_cancels_and_clears(
        self, capsys: pytest.CaptureFixture
    ) -> None:  # type: ignore[type-arg]
        fake_tasks = [mock.MagicMock() for _ in range(4)]
        hooks._TASKS.clear()
        hooks._TASKS.extend(fake_tasks)

        asyncio.get_event_loop().run_until_complete(on_shutdown(mock.MagicMock()))

        for task in fake_tasks:
            task.cancel.assert_called_once()
        assert hooks._TASKS == []
        captured = capsys.readouterr()
        assert "on_cleanup" in captured.out

    def test_on_startup_swallows_import_error(
        self, capsys: pytest.CaptureFixture
    ) -> None:  # type: ignore[type-arg]
        """Falha de import não deve propagar — apenas logar."""
        hooks._TASKS.clear()
        with mock.patch.dict(
            "sys.modules", {"backend.server": None}  # type: ignore[dict-item]
        ):
            # Não deve levantar
            asyncio.get_event_loop().run_until_complete(on_startup(mock.MagicMock()))
        captured = capsys.readouterr()
        assert "error" in captured.out.lower() or "on_startup" in captured.out
        hooks._TASKS.clear()
