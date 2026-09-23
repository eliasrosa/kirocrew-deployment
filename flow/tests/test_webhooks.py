"""Testes do handler de webhooks.

Sem rede — usa mock.patch.object nas funções de I/O (_find_open_pr_for_branch,
github_client.set_labels, github_client.get_state_comment,
github_client.upsert_state_comment).

Os testes HTTP usam aiohttp.test_utils.TestClient/TestServer diretamente,
sem depender do pytest-aiohttp (não é dependência do projeto).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from flow.webhooks import handler
from flow.webhooks.handler import (
    _extract_push_info,
    _verify_signature,
    create_app,
    handle_push_for_repo,
)

# ---------------------------------------------------------------------------
# _verify_signature
# ---------------------------------------------------------------------------

class TestVerifySignature:
    def test_sem_secret_sempre_valido(self) -> None:
        assert _verify_signature("", b"body", "sha256=qualquer") is True

    def test_sem_secret_e_sem_assinatura_valido(self) -> None:
        assert _verify_signature("", b"body", "") is True

    def test_assinatura_valida(self) -> None:
        body = b'{"event": "push"}'
        secret = "supersecret"
        mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert _verify_signature(secret, body, f"sha256={mac}") is True

    def test_assinatura_invalida(self) -> None:
        assert _verify_signature("secret", b"body", "sha256=errada") is False

    def test_header_sem_prefixo_invalido(self) -> None:
        assert _verify_signature("secret", b"body", "abc123") is False


# ---------------------------------------------------------------------------
# _extract_push_info
# ---------------------------------------------------------------------------

class TestExtractPushInfo:
    def test_extrai_repo_e_branch(self) -> None:
        payload = {
            "repository": {"full_name": "owner/repo"},
            "ref": "refs/heads/feat/issue-41",
        }
        result = _extract_push_info(payload)
        assert result == ("owner/repo", "feat/issue-41")

    def test_tag_nao_e_branch(self) -> None:
        payload = {
            "repository": {"full_name": "owner/repo"},
            "ref": "refs/tags/v1.0.0",
        }
        assert _extract_push_info(payload) is None

    def test_sem_repo_retorna_none(self) -> None:
        payload = {"ref": "refs/heads/main"}
        assert _extract_push_info(payload) is None

    def test_sem_ref_retorna_none(self) -> None:
        payload = {"repository": {"full_name": "owner/repo"}}
        assert _extract_push_info(payload) is None

    def test_branch_main(self) -> None:
        payload = {
            "repository": {"full_name": "org/api-gw"},
            "ref": "refs/heads/main",
        }
        result = _extract_push_info(payload)
        assert result == ("org/api-gw", "main")


# ---------------------------------------------------------------------------
# handle_push_for_repo
# ---------------------------------------------------------------------------

def _make_pr(number: int = 42, labels: list[str] | None = None) -> dict:
    return {
        "number": number,
        "title": "[api-gw2] Fix urgente",
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "url": f"https://github.com/owner/repo/pull/{number}",
    }


class TestHandlePushForRepo:
    def test_remove_reviewed_quando_presente(self) -> None:
        pr = _make_pr(labels=["flow:review-waiting", "flow:reviewed", "phase-1"])
        with (
            mock.patch.object(handler, "_find_open_pr_for_branch", return_value=pr),
            mock.patch.object(handler.github_client, "set_labels") as mock_set,
            mock.patch.object(handler.github_client, "get_state_comment", return_value=None),
            mock.patch.object(handler.github_client, "upsert_state_comment"),
        ):
            result = handle_push_for_repo("owner/repo", "feat/issue-41")

        assert result is True
        mock_set.assert_called_once_with(
            "owner/repo",
            "42",
            ["flow:review-waiting", "phase-1"],
        )

    def test_nao_remove_se_reviewed_ausente(self) -> None:
        pr = _make_pr(labels=["flow:review-waiting", "phase-1"])
        with mock.patch.object(handler, "_find_open_pr_for_branch", return_value=pr):
            result = handle_push_for_repo("owner/repo", "feat/issue-41")

        assert result is False

    def test_retorna_false_sem_pr_aberto(self) -> None:
        with mock.patch.object(handler, "_find_open_pr_for_branch", return_value=None):
            result = handle_push_for_repo("owner/repo", "feat/sem-pr")

        assert result is False

    def test_loga_e_retorna_false_em_provider_error(self) -> None:
        from flow.ports.issue_provider import ProviderError

        pr = _make_pr(labels=["flow:review-waiting", "flow:reviewed"])
        with (
            mock.patch.object(handler, "_find_open_pr_for_branch", return_value=pr),
            mock.patch.object(
                handler.github_client,
                "set_labels",
                side_effect=ProviderError("falhou"),
            ),
        ):
            result = handle_push_for_repo("owner/repo", "feat/issue-41")

        assert result is False

    def test_auditoria_best_effort_nao_bloqueia(self) -> None:
        """Mesmo com erro na auditoria, o retorno deve ser True."""
        pr = _make_pr(labels=["flow:review-waiting", "flow:reviewed"])
        with (
            mock.patch.object(handler, "_find_open_pr_for_branch", return_value=pr),
            mock.patch.object(handler.github_client, "set_labels"),
            mock.patch.object(
                handler.github_client,
                "get_state_comment",
                side_effect=Exception("falha de auditoria"),
            ),
        ):
            result = handle_push_for_repo("owner/repo", "feat/issue-41")

        assert result is True

    def test_preserva_outras_labels(self) -> None:
        pr = _make_pr(labels=[
            "flow:review-waiting",
            "flow:reviewed",
            "phase-1",
            "flow:feature",
        ])
        captured: list[list[str]] = []
        with (
            mock.patch.object(handler, "_find_open_pr_for_branch", return_value=pr),
            mock.patch.object(
                handler.github_client,
                "set_labels",
                side_effect=lambda _repo, _key, labels: captured.append(labels),
            ),
            mock.patch.object(handler.github_client, "get_state_comment", return_value=None),
            mock.patch.object(handler.github_client, "upsert_state_comment"),
        ):
            handle_push_for_repo("owner/repo", "feat/issue-41")

        assert captured[0] == ["flow:review-waiting", "phase-1", "flow:feature"]


# ---------------------------------------------------------------------------
# Helpers para testes HTTP (sem pytest-aiohttp)
# ---------------------------------------------------------------------------

def _run_async(coro: object) -> tuple[int, str]:
    """Executa uma coroutine no event loop padrão do teste."""
    return asyncio.get_event_loop().run_until_complete(coro)  # type: ignore[arg-type, return-value]


def _push_payload(repo: str = "owner/repo", branch: str = "feat/issue-41") -> bytes:
    return json.dumps({
        "repository": {"full_name": repo},
        "ref": f"refs/heads/{branch}",
    }).encode()


async def _post(app: web.Application, path: str, data: bytes, headers: dict) -> tuple[int, str]:  # type: ignore[type-arg]
    """Envia POST para o app e retorna (status, body_text)."""
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(path, data=data, headers=headers)
        text = await resp.text()
        return resp.status, text


# ---------------------------------------------------------------------------
# Handler aiohttp — testes HTTP
# ---------------------------------------------------------------------------

class TestWebhookRoute:
    def test_ping_retorna_200(self) -> None:
        app = create_app(webhook_secret="")
        status, text = _run_async(_post(app, "/webhook", b"{}", {"X-GitHub-Event": "ping"}))
        assert status == 200
        assert text == "pong"

    def test_evento_desconhecido_ignorado(self) -> None:
        app = create_app(webhook_secret="")
        status, text = _run_async(
            _post(app, "/webhook", b"{}", {"X-GitHub-Event": "pull_request"})
        )
        assert status == 200
        assert "ignored" in text

    def test_push_sem_pr_retorna_no_action(self) -> None:
        app = create_app(webhook_secret="")
        with mock.patch.object(handler, "_find_open_pr_for_branch", return_value=None):
            status, text = _run_async(
                _post(app, "/webhook", _push_payload(), {"X-GitHub-Event": "push"})
            )
        assert status == 200
        assert text == "no action"

    def test_push_com_reviewed_retorna_removed(self) -> None:
        app = create_app(webhook_secret="")
        pr = _make_pr(labels=["flow:review-waiting", "flow:reviewed"])
        with (
            mock.patch.object(handler, "_find_open_pr_for_branch", return_value=pr),
            mock.patch.object(handler.github_client, "set_labels"),
            mock.patch.object(handler.github_client, "get_state_comment", return_value=None),
            mock.patch.object(handler.github_client, "upsert_state_comment"),
        ):
            status, text = _run_async(
                _post(app, "/webhook", _push_payload(), {"X-GitHub-Event": "push"})
            )
        assert status == 200
        assert text == "reviewed label removed"

    def test_json_invalido_retorna_400(self) -> None:
        app = create_app(webhook_secret="")
        status, _ = _run_async(
            _post(app, "/webhook", b"nao-e-json", {"X-GitHub-Event": "push"})
        )
        assert status == 400

    def test_assinatura_invalida_retorna_401(self) -> None:
        app = create_app(webhook_secret="supersecret")
        status, _ = _run_async(
            _post(
                app,
                "/webhook",
                b"{}",
                {
                    "X-GitHub-Event": "ping",
                    "X-Hub-Signature-256": "sha256=errada",
                },
            )
        )
        assert status == 401

    def test_assinatura_valida_aceita(self) -> None:
        secret = "minhachave"
        body = _push_payload()
        mac = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        app = create_app(webhook_secret=secret)

        with mock.patch.object(handler, "_find_open_pr_for_branch", return_value=None):
            status, _ = _run_async(
                _post(
                    app,
                    "/webhook",
                    body,
                    {
                        "X-GitHub-Event": "push",
                        "X-Hub-Signature-256": f"sha256={mac}",
                    },
                )
            )
        assert status == 200
