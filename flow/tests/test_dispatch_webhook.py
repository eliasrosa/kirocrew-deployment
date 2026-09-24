"""Testes do transporte de dispatch via webhook (issue #212).

Cobre ``deployment.deployment._post_agent_session`` — o helper compartilhado
que os quatro dispatchers (_dispatch/_dispatch_rework/_dispatch_conflict_resolver/
_dispatch_reviewer) usam para acordar sessões de agente:

  1. Quando ``KIROCREW_WEBHOOK_TOKEN`` está setado, faz POST ao webhook do
     dashboard (``KIROCREW_WEBHOOK_URL``) com header ``Authorization: Bearer``
     e corpo JSON correto.
  2. Quando o token está vazio, cai no fallback loopback interno (/api/chat)
     e NÃO chama o webhook; num ctx sem ``_port``/``_secret`` (ScriptContext)
     não lança ``AttributeError``.
  3. O scan é zero-token: numa fila vazia (nenhum candidato), nenhum dispatch/
     webhook é acionado.

NUNCA toca um socket real — o POST é sempre mockado.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Literal
from unittest import mock

_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from deployment.deployment import (  # noqa: E402
    _dispatch_reviewer,
    _post_agent_session,
    _webhook_token,
    _webhook_url,
    run_dev,
)

_WEBHOOK_URL_DEFAULT = "http://localhost:5478/api/hooks/agent"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_script_ctx() -> mock.MagicMock:
    """ctx de cron ``script``-based: NÃO tem _port/_secret (causa da issue #212)."""
    ctx = mock.MagicMock(spec=["notify", "job"])
    ctx.job.id = "test-job"
    return ctx


def _make_message_ctx() -> mock.MagicMock:
    """ctx de cron ``message``-based: tem _port/_secret (comportamento legado)."""
    ctx = mock.MagicMock()
    ctx._port = 5000
    ctx._secret = "s3cr3t"
    ctx.job.id = "test-job"
    return ctx


def _base_config(**overrides: object) -> dict:
    cfg: dict = {
        "repos": ["owner/repo"],
        "auto_dispatch": True,
        "max_concurrent_tasks": 2,
        "one_per_repo": False,
        "notify_chat_id": "",
        "squad_id": "test",
        "issue_provider": "github",
        "dev_root": "/tmp/dev",
        "agent": "kirocrew",
    }
    cfg.update(overrides)
    return cfg


def _make_scan_result(state_label: str) -> object:
    from flow.domain.gates import WorkItem
    from flow.domain.state import parse_modifiers, parse_state
    from flow.scan.scanner import ScanResult

    labels_set = frozenset([state_label])
    from flow.domain.state import State

    state = parse_state(labels_set)
    mods = parse_modifiers(labels_set)
    return ScanResult(
        item=WorkItem(
            key="https://github.com/owner/repo/issues/42",
            title="[owner/repo] Test issue",
            labels=labels_set,
        ),
        current_state=state,
        modifiers=mods,
        dispatch_candidate=(state is State.DEVELOP_WAITING),
        spec_valid=None,
        changed=True,
        reason="test",
    )


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

class TestWebhookConfig:
    def test_webhook_url_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_WEBHOOK_URL", raising=False)
        assert _webhook_url() == _WEBHOOK_URL_DEFAULT

    def test_webhook_url_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_WEBHOOK_URL", "https://example.test/hook")
        assert _webhook_url() == "https://example.test/hook"

    def test_webhook_token_default_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("KIROCREW_WEBHOOK_TOKEN", raising=False)
        assert _webhook_token() == ""

    def test_webhook_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KIROCREW_WEBHOOK_TOKEN", "tok-123")
        assert _webhook_token() == "tok-123"


# ---------------------------------------------------------------------------
# _post_agent_session — webhook path
# ---------------------------------------------------------------------------

class TestPostAgentSessionWebhook:
    def test_posts_to_webhook_with_bearer_and_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Com token setado: POST ao webhook com Authorization Bearer e corpo correto."""
        monkeypatch.setenv("KIROCREW_WEBHOOK_TOKEN", "tok-abc")
        monkeypatch.setenv("KIROCREW_WEBHOOK_URL", "https://hooks.test/agent")

        captured: dict = {}

        class _FakeResp:
            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *a: object) -> Literal[False]:
                return False

            def read(self, _n: int = -1) -> bytes:
                return b""

        def _fake_urlopen(req: object, timeout: float = 0) -> _FakeResp:
            captured["url"] = req.full_url  # type: ignore[attr-defined]
            captured["headers"] = dict(req.headers)  # type: ignore[attr-defined]
            captured["data"] = req.data  # type: ignore[attr-defined]
            captured["method"] = req.get_method()  # type: ignore[attr-defined]
            captured["timeout"] = timeout
            return _FakeResp()

        ctx = _make_script_ctx()
        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen) as mock_open:
            _post_agent_session(
                ctx, "Implemente a issue #42", slot="esteira-repo-42",
                cfg=_base_config(agent="crewflow-dev"),
            )

        mock_open.assert_called_once()
        assert captured["url"] == "https://hooks.test/agent"
        assert captured["method"] == "POST"
        # Header keys são capitalizadas por urllib.Request
        assert captured["headers"]["Authorization"] == "Bearer tok-abc"
        assert captured["headers"]["Content-type"] == "application/json"
        body = json.loads(captured["data"])
        assert body["message"] == "Implemente a issue #42"
        assert body["slot"] == "esteira-repo-42"
        assert body["agent"] == "crewflow-dev"
        assert body["memory_mode"] == "temporary"
        assert captured["timeout"] == 10

    def test_webhook_default_url_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_WEBHOOK_TOKEN", "tok-abc")
        monkeypatch.delenv("KIROCREW_WEBHOOK_URL", raising=False)

        captured: dict = {}

        def _fake_urlopen(req: object, timeout: float = 0):  # type: ignore[no-untyped-def]
            captured["url"] = req.full_url  # type: ignore[attr-defined]
            return mock.MagicMock(
                __enter__=lambda s: s, __exit__=lambda *a: False, read=lambda n=-1: b""
            )

        ctx = _make_script_ctx()
        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _post_agent_session(ctx, "msg", slot="slot-1", cfg=_base_config())

        assert captured["url"] == _WEBHOOK_URL_DEFAULT

    def test_webhook_agent_defaults_to_kirocrew(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KIROCREW_WEBHOOK_TOKEN", "tok-abc")
        captured: dict = {}

        def _fake_urlopen(req: object, timeout: float = 0):  # type: ignore[no-untyped-def]
            captured["data"] = req.data  # type: ignore[attr-defined]
            return mock.MagicMock(
                __enter__=lambda s: s, __exit__=lambda *a: False, read=lambda n=-1: b""
            )

        ctx = _make_script_ctx()
        cfg = _base_config()
        cfg.pop("agent")
        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _post_agent_session(ctx, "msg", slot="slot-1", cfg=cfg)

        assert json.loads(captured["data"])["agent"] == "kirocrew"

    def test_webhook_swallows_exceptions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fire-and-forget: falha do POST ao webhook não propaga, retorna False."""
        monkeypatch.setenv("KIROCREW_WEBHOOK_TOKEN", "tok-abc")
        ctx = _make_script_ctx()
        with mock.patch(
            "urllib.request.urlopen", side_effect=OSError("connection refused")
        ):
            # Não deve levantar; sinaliza falha
            assert _post_agent_session(ctx, "msg", slot="slot-1", cfg=_base_config()) is False

    def test_webhook_returns_true_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST bem-sucedido ao webhook retorna True."""
        monkeypatch.setenv("KIROCREW_WEBHOOK_TOKEN", "tok-abc")
        ctx = _make_script_ctx()

        def _fake_urlopen(req: object, timeout: float = 0):  # type: ignore[no-untyped-def]
            return mock.MagicMock(
                __enter__=lambda s: s, __exit__=lambda *a: False, read=lambda n=-1: b""
            )

        with mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            assert _post_agent_session(ctx, "msg", slot="slot-1", cfg=_base_config()) is True


# ---------------------------------------------------------------------------
# _post_agent_session — fallback loopback path (token vazio)
# ---------------------------------------------------------------------------

class TestPostAgentSessionLoopbackFallback:
    def test_no_token_uses_loopback_not_webhook(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem token: usa urlopen para /api/chat/slots + /api/chat, NÃO chama o webhook endpoint."""
        monkeypatch.delenv("KIROCREW_WEBHOOK_TOKEN", raising=False)
        monkeypatch.setattr("os.path.exists", lambda p: False if ".local_secret" in str(p) else __import__("os.path", fromlist=["exists"]).exists(p))

        calls: list = []

        class _FakeResp:
            def __enter__(self) -> "_FakeResp":
                return self
            def __exit__(self, *a: object) -> bool:
                return False
            def read(self, _n: int = -1) -> bytes:
                return b""

        def fake_urlopen(req: object, timeout: float = 0) -> _FakeResp:
            calls.append(req.full_url)  # type: ignore[attr-defined]
            return _FakeResp()

        ctx = _make_message_ctx()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _post_agent_session(ctx, "msg", slot="slot-1", cfg=_base_config())

        # Step 1: criar slot
        assert any("api/chat/slots" in url for url in calls), f"api/chat/slots não chamado: {calls}"
        # Step 2: enviar mensagem
        assert any(url.endswith("/api/chat") for url in calls), f"api/chat não chamado: {calls}"
        # Não deve ter chamado o webhook endpoint
        assert not any("hooks/agent" in url for url in calls)

    def test_no_token_script_ctx_no_attribute_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem token, ctx sem _port/_secret e sem .local_secret: retorna False sem AttributeError."""
        monkeypatch.delenv("KIROCREW_WEBHOOK_TOKEN", raising=False)
        monkeypatch.setattr("os.path.exists", lambda p: False if ".local_secret" in str(p) else __import__("os.path", fromlist=["exists"]).exists(p))

        ctx = _make_script_ctx()  # sem _port/_secret
        with mock.patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = _post_agent_session(ctx, "msg", slot="slot-1", cfg=_base_config())
            # OSError no step 1 (criar slot) → retorna False
            assert result is False


# ---------------------------------------------------------------------------
# Scan zero-token — fila vazia não aciona dispatch/webhook
# ---------------------------------------------------------------------------

class TestZeroTokenScan:
    def test_empty_queue_no_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_dev com fila vazia: nenhum _dispatch e nenhum POST ao webhook."""
        monkeypatch.setenv("KIROCREW_WEBHOOK_TOKEN", "tok-abc")
        ctx = _make_script_ctx()

        with (
            mock.patch("deployment.deployment._load_config", return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
            mock.patch("urllib.request.urlopen") as mock_webhook,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_not_called()
        mock_webhook.assert_not_called()

    def test_candidate_triggers_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_dev com um candidato: _dispatch é chamado exatamente uma vez."""
        ctx = _make_script_ctx()
        result = _make_scan_result("flow:develop-waiting")

        with (
            mock.patch("deployment.deployment._load_config", return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("flow.adapters.github_client.edit_issue_labels"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Reviewer — sinalização de sucesso/falha do dispatch (issues #2/#3 do review)
# ---------------------------------------------------------------------------

class TestReviewerDispatchSignalling:
    """O reviewer é o único dispatcher que reporta falha ao operador via notify.

    Garante que a notify de sucesso só dispara quando o POST teve sucesso e que
    a notify de falha ('falha ao despachar reviewer') volta a disparar quando
    ``_post_agent_session`` sinaliza falha (regressão do review v1).
    """

    def _patches(self, pr_found: bool = True):  # type: ignore[no-untyped-def]
        pr_json = json.dumps([{"number": 7}]) if pr_found else "[]"
        gh_result = mock.MagicMock(returncode=0, stdout=pr_json)
        sha_result = mock.MagicMock(returncode=0, stdout="abc123")
        return mock.patch(
            "deployment.deployment.subprocess.run",
            side_effect=[gh_result, sha_result],
        )

    def test_reviewer_notifies_success_when_post_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_script_ctx()
        issue = {"number": 42, "title": "Test"}
        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=False),
            self._patches(pr_found=True),
            mock.patch(
                "deployment.deployment._reviewer_prompt", return_value="review this"
            ),
            mock.patch(
                "deployment.deployment._post_agent_session", return_value=True
            ) as mock_post,
        ):
            _dispatch_reviewer(ctx, "owner/repo", issue, _base_config())

        mock_post.assert_called_once()
        notify_msgs = [c.args[0] for c in ctx.notify.call_args_list]
        assert any("sessão one-shot do reviewer despachada" in m for m in notify_msgs)
        assert not any("falha ao despachar reviewer" in m for m in notify_msgs)

    def test_reviewer_notifies_failure_when_post_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ctx = _make_script_ctx()
        issue = {"number": 42, "title": "Test"}
        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=False),
            self._patches(pr_found=True),
            mock.patch(
                "deployment.deployment._reviewer_prompt", return_value="review this"
            ),
            mock.patch(
                "deployment.deployment._post_agent_session", return_value=False
            ) as mock_post,
        ):
            _dispatch_reviewer(ctx, "owner/repo", issue, _base_config())

        mock_post.assert_called_once()
        notify_msgs = [c.args[0] for c in ctx.notify.call_args_list]
        assert any("falha ao despachar reviewer" in m for m in notify_msgs)
        assert not any("sessão one-shot do reviewer despachada" in m for m in notify_msgs)
