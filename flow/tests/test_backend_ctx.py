"""Testes de unidade para BackendCronCtx.

Critérios de aceite (issue #149):
- ctx.notify(text) é síncrono e não lança exceção
- ctx.message retorna a string passada no construtor
- Sem gateway configurado, notify() usa o logger (sem HTTP)
- Com gateway configurado e indisponível, notify() faz fallback para log sem lançar
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest import mock

import pytest

# Garante que o raiz do repo está no path para importar backend/
_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.ctx import BackendCronCtx  # noqa: E402


class TestBackendCronCtxMessage:
    def test_message_default_is_empty_string(self) -> None:
        ctx = BackendCronCtx()
        assert ctx.message == ""

    def test_message_stores_custom_value(self) -> None:
        ctx = BackendCronCtx(message="hello world")
        assert ctx.message == "hello world"

    def test_message_is_mutable(self) -> None:
        ctx = BackendCronCtx()
        ctx.message = "updated"
        assert ctx.message == "updated"


class TestBackendCronCtxNotifyNoGateway:
    def test_notify_does_not_raise(self) -> None:
        ctx = BackendCronCtx()
        # Não deve lançar exceção nenhuma
        ctx.notify("test message")

    def test_notify_logs_info_when_no_gateway(self, caplog: pytest.LogCaptureFixture) -> None:
        ctx = BackendCronCtx()
        with caplog.at_level(logging.INFO, logger="backend.ctx"):
            ctx.notify("notification text")
        assert any("notification text" in r.message for r in caplog.records)

    def test_notify_with_empty_text_does_not_raise(self) -> None:
        ctx = BackendCronCtx()
        ctx.notify("")

    def test_notify_with_multiline_text(self, caplog: pytest.LogCaptureFixture) -> None:
        ctx = BackendCronCtx()
        msg = "line1\nline2\nline3"
        with caplog.at_level(logging.INFO, logger="backend.ctx"):
            ctx.notify(msg)
        assert any("line1" in r.message for r in caplog.records)


class TestBackendCronCtxNotifyWithGateway:
    def test_notify_falls_back_to_log_when_gateway_unreachable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Com porta configurada mas gateway offline, deve fazer fallback para log."""
        ctx = BackendCronCtx(gateway_port=19999)  # porta não usada
        with caplog.at_level(logging.DEBUG, logger="backend.ctx"):
            ctx.notify("fallback test")
        # Verifica que caiu no fallback (debug log de falha + info log de conteúdo)
        messages = " ".join(r.message for r in caplog.records)
        assert "fallback test" in messages or "gateway" in messages.lower()

    def test_notify_succeeds_via_gateway(self) -> None:
        """Com gateway simulado que responde 200, não deve lançar exceção."""
        ctx = BackendCronCtx(gateway_port=8080)
        mock_response = mock.MagicMock()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            ctx.notify("via gateway")
        # Não deve lançar


class TestBackendCronCtxDuckTyping:
    """Garante que BackendCronCtx satisfaz o contrato de duck-typing esperado pelo deployment.py."""

    def test_has_notify_callable(self) -> None:
        ctx = BackendCronCtx()
        assert callable(ctx.notify)

    def test_has_message_attribute(self) -> None:
        ctx = BackendCronCtx()
        assert hasattr(ctx, "message")
        assert isinstance(ctx.message, str)

    def test_notify_signature_accepts_single_string(self) -> None:
        ctx = BackendCronCtx()
        # Deve aceitar uma string posicional — exatamente o que deployment.py usa:
        # ctx.notify(summary)
        ctx.notify("single string argument")
