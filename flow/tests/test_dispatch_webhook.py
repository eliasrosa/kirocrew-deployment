"""Testes do transporte de dispatch via loopback interno (issue #212, #224).

Cobre ``deployment.deployment._post_agent_session`` — o helper compartilhado
que os quatro dispatchers (_dispatch/_dispatch_rework/_dispatch_conflict_resolver/
_dispatch_reviewer) usam para acordar sessões de agente:

  1. Faz POST /api/chat/slots para registrar o slot no gateway (idempotente).
  2. Faz POST /api/chat com X-Session-Key: dashboard:{slot} para criar sessão
     dashboard_esteira-* visível no sidebar.
  3. O scan é zero-token: numa fila vazia (nenhum candidato), nenhum dispatch/
     POST é acionado.

NUNCA toca um socket real — os POSTs são sempre mockados.
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
    run_dev,
)

_WEBHOOK_URL_DEFAULT = "http://localhost:5478/api/hooks/agent"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_script_ctx() -> mock.MagicMock:
    """ctx de cron ``script``-based: tem _port/_secret via ScriptContext."""
    ctx = mock.MagicMock()
    ctx._port = 5000
    ctx._secret = "s3cr3t"
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
# _post_agent_session — fluxo create slot + send
# ---------------------------------------------------------------------------

class TestPostAgentSessionLoopback:
    def _fake_urlopen(self, calls: list) -> object:
        class _FakeResp:
            def __enter__(self) -> "_FakeResp":
                return self
            def __exit__(self, *a: object) -> bool:
                return False
            def read(self, _n: int = -1) -> bytes:
                return b""

        def fake(req: object, timeout: float = 0) -> _FakeResp:
            calls.append(req.full_url)  # type: ignore[attr-defined]
            return _FakeResp()

        return fake

    def test_faz_post_create_slot_e_chat(self) -> None:
        """Dois POSTs em sequência: /api/chat/slots depois /api/chat."""
        calls: list = []
        ctx = _make_script_ctx()
        with mock.patch("urllib.request.urlopen", side_effect=self._fake_urlopen(calls)):
            result = _post_agent_session(ctx, "msg", slot="esteira-repo-42", cfg=_base_config())

        assert result is True
        assert any("api/chat/slots" in url for url in calls), f"slots não chamado: {calls}"
        assert any(url.endswith("/api/chat") for url in calls), f"chat não chamado: {calls}"

    def test_slot_create_antes_do_chat(self) -> None:
        """O /api/chat/slots deve ser chamado ANTES do /api/chat."""
        calls: list = []
        ctx = _make_script_ctx()
        with mock.patch("urllib.request.urlopen", side_effect=self._fake_urlopen(calls)):
            _post_agent_session(ctx, "msg", slot="esteira-repo-42", cfg=_base_config())

        slots_idx = next(i for i, u in enumerate(calls) if "api/chat/slots" in u)
        chat_idx = next(i for i, u in enumerate(calls) if u.endswith("/api/chat"))
        assert slots_idx < chat_idx

    def test_chat_envia_corpo_correto(self) -> None:
        """O POST /api/chat inclui message, slot, agent e memory_mode."""
        captured: dict = {}

        class _FakeResp:
            def __enter__(self) -> "_FakeResp": return self
            def __exit__(self, *a: object) -> bool: return False
            def read(self, _n: int = -1) -> bytes: return b""

        call_count = 0

        def fake(req: object, timeout: float = 0) -> _FakeResp:  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            url = req.full_url  # type: ignore[attr-defined]
            if url.endswith("/api/chat"):
                captured["data"] = req.data  # type: ignore[attr-defined]
                captured["session_key"] = req.get_header("X-session-key")  # type: ignore[attr-defined]
            return _FakeResp()

        ctx = _make_script_ctx()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            _post_agent_session(ctx, "Implemente #42", slot="esteira-repo-42",
                                cfg=_base_config(agent="crewflow-dev"))

        body = json.loads(captured["data"])
        assert body["message"] == "Implemente #42"
        assert body["slot"] == "esteira-repo-42"
        assert body["agent"] == "crewflow-dev"
        assert "memory_mode" not in body  # memory_mode vai no create slot, não no send
        assert captured["session_key"] == "dashboard:esteira-repo-42"

    def test_retorna_false_se_create_slot_falha(self) -> None:
        """Falha no step 1 (criar slot) → retorna False sem chamar /api/chat."""
        calls: list = []
        ctx = _make_script_ctx()

        class _FakeResp:
            def __enter__(self) -> "_FakeResp": return self
            def __exit__(self, *a: object) -> bool: return False
            def read(self, _n: int = -1) -> bytes: return b""

        def fake(req: object, timeout: float = 0) -> _FakeResp:  # type: ignore[no-untyped-def]
            url = req.full_url  # type: ignore[attr-defined]
            calls.append(url)
            if "api/chat/slots" in url:
                raise OSError("connection refused")
            return _FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            result = _post_agent_session(ctx, "msg", slot="slot-1", cfg=_base_config())

        assert result is False
        assert not any(u.endswith("/api/chat") for u in calls), "chat não deve ser chamado se slots falhou"

    def test_retorna_false_se_chat_falha(self) -> None:
        """Falha no step 2 (chat) → retorna False."""
        ctx = _make_script_ctx()

        class _FakeResp:
            def __enter__(self) -> "_FakeResp": return self
            def __exit__(self, *a: object) -> bool: return False
            def read(self, _n: int = -1) -> bytes: return b""

        def fake(req: object, timeout: float = 0) -> _FakeResp:  # type: ignore[no-untyped-def]
            url = req.full_url  # type: ignore[attr-defined]
            if url.endswith("/api/chat"):
                raise OSError("connection refused")
            return _FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            result = _post_agent_session(ctx, "msg", slot="slot-1", cfg=_base_config())

        assert result is False

    def test_usa_local_secret_quando_ctx_nao_tem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ctx sem _secret → lê de ~/.kiro/crew/.local_secret."""
        secret_file = tmp_path / ".local_secret"
        secret_file.write_text("my-local-secret")

        ctx = mock.MagicMock(spec=["notify", "job"])
        ctx._port = 5000
        ctx.job.id = "test-job"

        monkeypatch.setattr(
            "os.path.expanduser",
            lambda p: str(secret_file) if ".local_secret" in p else p,
        )

        captured: dict = {}

        class _FakeResp:
            def __enter__(self) -> "_FakeResp": return self
            def __exit__(self, *a: object) -> bool: return False
            def read(self, _n: int = -1) -> bytes: return b""

        def fake(req: object, timeout: float = 0) -> _FakeResp:  # type: ignore[no-untyped-def]
            url = req.full_url  # type: ignore[attr-defined]
            if "api/chat/slots" in url:
                captured["secret"] = req.get_header("X-internal-secret")  # type: ignore[attr-defined]
            return _FakeResp()

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            _post_agent_session(ctx, "msg", slot="slot-1", cfg=_base_config())

        assert captured.get("secret") == "my-local-secret"


# ---------------------------------------------------------------------------
# Scan zero-token — fila vazia não aciona dispatch/POST
# ---------------------------------------------------------------------------

class TestZeroTokenScan:
    def test_empty_queue_no_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_dev com fila vazia: nenhum _dispatch e nenhum POST."""
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "s3cr3t"
        ctx.job.id = "test-job"

        with (
            mock.patch("deployment.deployment._load_config", return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
            mock.patch("urllib.request.urlopen") as mock_urlopen,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_candidate_triggers_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """run_dev com um candidato: _dispatch é chamado exatamente uma vez."""
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "s3cr3t"
        ctx.job.id = "test-job"
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
# Reviewer — sinalização de sucesso/falha do dispatch
# ---------------------------------------------------------------------------

class TestReviewerDispatchSignalling:
    """O reviewer é o único dispatcher que reporta falha ao operador via notify."""

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
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "s3cr3t"
        ctx.job.id = "test-job"
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
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "s3cr3t"
        ctx.job.id = "test-job"
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
