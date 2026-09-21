"""Handler de webhooks do GitHub para o KiroCrew Flow.

Responsabilidade: receber eventos ``push`` do GitHub em PRs abertos e
remover a label ``crewflow:reviewed`` da issue correspondente.

Por que isso é necessário:
    O lock anti-loop ``crewflow:reviewed`` impede que o kiro-reviewer
    processe a mesma revisão duas vezes. Quando o dev faz um novo push,
    a label deve ser removida para que o próximo ciclo do scan dispare
    uma nova análise.

Fluxo do evento:
    1. GitHub envia POST /webhook com X-GitHub-Event: push
    2. O handler valida a assinatura HMAC-SHA256 (se WEBHOOK_SECRET configurado)
    3. Identifica o PR aberto associado ao branch do push via GitHub API
    4. Remove ``crewflow:reviewed`` das labels da issue se presente
    5. Registra a ação no comentário de auditoria <!-- KIRO-FLOW-STATE -->

Servidor:
    Usar ``create_app()`` para criar o Application aiohttp.
    Para rodar standalone: ``python -m flow.webhooks.server``
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

from aiohttp import web

from flow.adapters import github_client
from flow.audit.state_comment import StateComment, parse, render
from flow.ports.issue_provider import ProviderError

logger = logging.getLogger(__name__)

LABEL_REVIEWED = "flow:reviewed"


# ---------------------------------------------------------------------------
# Validação de assinatura HMAC-SHA256
# ---------------------------------------------------------------------------

def _verify_signature(secret: str, body: bytes, sig_header: str) -> bool:
    """Verifica a assinatura X-Hub-Signature-256 do GitHub.

    Retorna True se a assinatura bater ou se o secret for vazio (desenvolvimento).
    """
    if not secret:
        return True
    if not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    received = sig_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


# ---------------------------------------------------------------------------
# Extração do repo e branch do payload de push
# ---------------------------------------------------------------------------

def _extract_push_info(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Extrai (owner/repo, branch_name) de um payload de push.

    Retorna None se o payload não for um push em branch de PR candidato.
    """
    repo_full = (payload.get("repository") or {}).get("full_name", "")
    ref = payload.get("ref", "")  # ex: "refs/heads/feat/issue-41"
    if not repo_full or not ref.startswith("refs/heads/"):
        return None
    branch = ref.removeprefix("refs/heads/")
    return repo_full, branch


# ---------------------------------------------------------------------------
# Busca de PRs abertos para o branch via gh CLI
# ---------------------------------------------------------------------------

def _find_open_pr_for_branch(repo: str, branch: str) -> dict[str, Any] | None:
    """Busca o PR aberto associado ao branch via gh CLI.

    Retorna o dict do PR (com number, title, labels, url) ou None.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", repo,
                "--head", branch,
                "--state", "open",
                "--json", "number,title,labels,url,issueType",
                "--limit", "1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        prs = json.loads(result.stdout)
        return prs[0] if prs else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("webhook: falha ao buscar PR para %s/%s: %s", repo, branch, exc)
        return None


# ---------------------------------------------------------------------------
# Lógica principal: remover crewflow:reviewed
# ---------------------------------------------------------------------------

def handle_push_for_repo(repo: str, branch: str) -> bool:
    """Processa um evento push: remove crewflow:reviewed do PR aberto.

    Retorna True se a label foi removida, False caso contrário.

    Não lança exceções — erros são logados.
    """
    pr = _find_open_pr_for_branch(repo, branch)
    if pr is None:
        logger.debug("webhook: nenhum PR aberto para %s em %s", branch, repo)
        return False

    pr_number = str(pr["number"])
    pr_labels = [lbl["name"] for lbl in pr.get("labels", [])]

    if LABEL_REVIEWED not in pr_labels:
        logger.debug(
            "webhook: PR #%s em %s não tem %s — nada a fazer",
            pr_number, repo, LABEL_REVIEWED,
        )
        return False

    new_labels = [lbl for lbl in pr_labels if lbl != LABEL_REVIEWED]

    try:
        github_client.set_labels(repo, pr_number, new_labels)
        logger.info(
            "webhook: removida %s do PR #%s em %s (push em %s)",
            LABEL_REVIEWED, pr_number, repo, branch,
        )
    except ProviderError as exc:
        logger.error(
            "webhook: falha ao remover %s do PR #%s: %s",
            LABEL_REVIEWED, pr_number, exc,
        )
        return False

    # Registra a ação no comentário de auditoria
    _record_audit(repo, pr_number, pr.get("title", ""), pr_labels)
    return True


def _record_audit(repo: str, pr_number: str, pr_title: str, old_labels: list[str]) -> None:
    """Registra a remoção no comentário de auditoria <!-- KIRO-FLOW-STATE -->."""
    try:
        from datetime import UTC, datetime

        existing_body = github_client.get_state_comment(repo, pr_number)
        sc = parse(existing_body) if existing_body else None

        if sc is None:
            sc = StateComment(
                workflow="desconhecido",
                current_node="review",
                status="awaiting-review",
                repo=repo.split("/")[-1],
            )

        sc.add_transition(
            from_state="review+reviewed",
            to_state="review",
            actor="webhook:push",
            when=datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M"),
        )
        body = render(sc)
        github_client.upsert_state_comment(repo, pr_number, body)
    except Exception as exc:
        # Auditoria é best-effort — não bloqueia o fluxo principal
        logger.warning("webhook: falha ao registrar auditoria para PR #%s: %s", pr_number, exc)


# ---------------------------------------------------------------------------
# Handler aiohttp
# ---------------------------------------------------------------------------

async def _handle_webhook(request: web.Request) -> web.Response:
    """Handler POST /webhook — processa eventos push do GitHub."""
    secret: str = request.app["webhook_secret"]

    body = await request.read()

    # Valida assinatura HMAC se secret configurado
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not _verify_signature(secret, body, sig):
        logger.warning("webhook: assinatura inválida — rejeitando request")
        return web.Response(status=401, text="invalid signature")

    event = request.headers.get("X-GitHub-Event", "")

    if event == "ping":
        return web.Response(status=200, text="pong")

    if event != "push":
        # Ignora eventos que não são push (PR opened, etc.) sem erro
        return web.Response(status=200, text=f"event {event!r} ignored")

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError:
        return web.Response(status=400, text="invalid JSON")

    info = _extract_push_info(payload)
    if info is None:
        return web.Response(status=200, text="push ignored (no branch ref)")

    repo, branch = info
    removed = handle_push_for_repo(repo, branch)

    return web.Response(
        status=200,
        text="reviewed label removed" if removed else "no action",
    )


# ---------------------------------------------------------------------------
# Factory da aplicação aiohttp
# ---------------------------------------------------------------------------

def create_app(webhook_secret: str = "") -> web.Application:
    """Cria e configura o Application aiohttp.

    Args:
        webhook_secret: segredo HMAC para validar assinaturas X-Hub-Signature-256.
                        Vazio = sem validação (uso em desenvolvimento/testes).

    Returns:
        Application aiohttp pronto para uso com ``web.run_app`` ou testes.
    """
    app = web.Application()
    app["webhook_secret"] = webhook_secret
    app.router.add_post("/webhook", _handle_webhook)
    return app
