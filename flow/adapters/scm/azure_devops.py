"""Transport Azure DevOps — fachada REST sobre a API de Pull Requests.

Implementa a mesma superfície do ``GithubTransport`` (módulo
``flow.adapters.scm.github``), mas usando a REST API do Azure DevOps
em vez do ``gh`` CLI.

Autenticação
------------
Lê o PAT de ``os.environ["AZURE_DEVOPS_PAT"]``.
Usa Basic auth: ``Authorization: Basic base64(:PAT)``.

Configuração por repo (squad YAML)
------------------------------------
::

    repos:
      - name: kdop/api-gateway2
        scm: azure_devops
        azure_org: https://dev.azure.com/kdop
        azure_project: PlataformaCogna-MKTP-MVP
        azure_repo: voomp-creators-api-gateway2

Tratamento de erros
-------------------
Todos os erros de rede/API são normalizados para ``ProviderError`` (e
subclasses) antes de sair deste módulo.

Isolamento de testes
--------------------
O ponto de injeção é ``_request``: patches sobre esse helper evitam
qualquer I/O real nos testes unitários.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Any
from urllib.error import HTTPError, URLError

from flow.ports.issue_provider import (
    ProviderError,
    ProviderPermissionError,
    ProviderSetupError,
)

# ---------------------------------------------------------------------------
# Configuração e auth
# ---------------------------------------------------------------------------

_ENV_PAT = "AZURE_DEVOPS_PAT"


def _pat() -> str:
    """Lê o PAT do ambiente. Lança ProviderSetupError se ausente."""
    pat = os.environ.get(_ENV_PAT, "").strip()
    if not pat:
        raise ProviderSetupError(
            f"Variável de ambiente {_ENV_PAT!r} não definida — "
            "gere um PAT em dev.azure.com/_usersSettings/tokens e exporte-o."
        )
    return pat


def _auth_header(pat: str) -> str:
    """Gera o header Basic para o PAT do Azure DevOps.

    O Azure DevOps exige ``:<PAT>`` (sem usuário) em base64 quando a
    identidade do usuário é resolvida pelo token.
    """
    token = base64.b64encode(f":{pat}".encode()).decode()
    return f"Basic {token}"


# ---------------------------------------------------------------------------
# Camada de I/O (patchável nos testes)
# ---------------------------------------------------------------------------

def _request(
    method: str,
    url: str,
    body: dict[str, Any] | list[dict[str, Any]] | None = None,
    *,
    timeout: int = 30,
) -> Any:
    """Executa uma requisição HTTP autenticada à API do Azure DevOps.

    Parâmetros
    ----------
    method  : verbo HTTP (GET, POST, PUT, PATCH)
    url     : URL completa com ``?api-version=...``
    body    : payload JSON (serializado automaticamente)

    Retorna o payload JSON decodificado (dict ou list).

    Lança
    -----
    ProviderSetupError    — PAT ausente ou inválido (401)
    ProviderPermissionError — sem permissão (403)
    ProviderError         — outros erros HTTP ou de rede
    """
    pat = _pat()
    headers = {
        "Authorization": _auth_header(pat),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode()

    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw)
    except HTTPError as exc:
        body_bytes = exc.read()
        detail = body_bytes.decode(errors="replace")[:300] if body_bytes else ""
        if exc.code == 401:
            raise ProviderSetupError(
                f"Azure DevOps: autenticação falhou (401) — PAT inválido ou expirado. {detail}"
            ) from exc
        if exc.code == 403:
            raise ProviderPermissionError(
                f"Azure DevOps: sem permissão (403). {detail}"
            ) from exc
        if exc.code == 404:
            raise ProviderError(
                f"Azure DevOps: recurso não encontrado (404): {url}"
            ) from exc
        raise ProviderError(
            f"Azure DevOps: HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise ProviderError(
            f"Azure DevOps: erro de rede: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"Azure DevOps: resposta não-JSON: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Helpers de URL
# ---------------------------------------------------------------------------

_API_VERSION = "7.1"


def _pr_url(org: str, project: str, repo: str, suffix: str = "") -> str:
    """Monta a URL base de PRs para o repo."""
    base = f"{org.rstrip('/')}/{project}/_apis/git/repositories/{repo}/pullRequests"
    if suffix:
        return f"{base}/{suffix}?api-version={_API_VERSION}"
    return f"{base}?api-version={_API_VERSION}"


def _refs_url(org: str, project: str, repo: str, branch: str) -> str:
    """URL para deletar um ref (branch)."""
    return (
        f"{org.rstrip('/')}/{project}/_apis/git/repositories/{repo}"
        f"/refs?filter=heads/{branch}&api-version={_API_VERSION}"
    )


def _threads_url(org: str, project: str, repo: str, pr_number: int) -> str:
    return (
        f"{org.rstrip('/')}/{project}/_apis/git/repositories/{repo}"
        f"/pullRequests/{pr_number}/threads?api-version={_API_VERSION}"
    )


# ---------------------------------------------------------------------------
# API pública (mesma superfície que github_transport)
# ---------------------------------------------------------------------------

def create_pull_request(
    org: str,
    project: str,
    repo: str,
    branch: str,
    title: str,
    body: str,
    reviewers: list[str] | None = None,
    target_branch: str = "main",
) -> dict:
    """Abre um Pull Request no Azure DevOps.

    Parâmetros
    ----------
    org, project, repo : coordenadas do repositório no Azure DevOps
    branch             : branch de origem (head)
    title              : título do PR
    body               : descrição (markdown)
    reviewers          : lista de UUIDs ou UPNs dos revisores
    target_branch      : branch de destino (default: main)

    Retorna o dict do PR criado (payload cru da API).
    """
    payload: dict[str, Any] = {
        "title": title,
        "description": body,
        "sourceRefName": f"refs/heads/{branch}",
        "targetRefName": f"refs/heads/{target_branch}",
    }
    if reviewers:
        payload["reviewers"] = [{"id": r} for r in reviewers]

    url = _pr_url(org, project, repo)
    return _request("POST", url, body=payload)


def list_pull_requests(
    org: str,
    project: str,
    repo: str,
    state: str = "active",
    source_branch: str | None = None,
) -> list:
    """Lista PRs do repositório.

    Parâmetros
    ----------
    state         : ``active`` | ``completed`` | ``abandoned``
    source_branch : filtra por branch de origem (sem ``refs/heads/``)
    """
    params = f"searchCriteria.status={state}"
    if source_branch:
        params += f"&searchCriteria.sourceRefName=refs/heads/{source_branch}"
    url = f"{org.rstrip('/')}/{project}/_apis/git/repositories/{repo}/pullRequests?{params}&api-version={_API_VERSION}"
    result = _request("GET", url)
    return result.get("value", [])


def get_pull_request(
    org: str,
    project: str,
    repo: str,
    pr_number: int,
) -> dict:
    """Retorna os detalhes de um PR (estado, mergeable, head SHA).

    Campos relevantes no retorno:
      - ``pullRequestId``  : int
      - ``status``         : str (``active``, ``completed``, ``abandoned``)
      - ``mergeStatus``    : str (``succeeded``, ``conflicts``, ``notSet``, ...)
      - ``lastMergeSourceCommit.commitId`` : str (head SHA)
    """
    url = _pr_url(org, project, repo, str(pr_number))
    return _request("GET", url)


def merge_pull_request(
    org: str,
    project: str,
    repo: str,
    pr_number: int,
    merge_strategy: str = "squash",
) -> dict:
    """Completa (merge) um PR.

    Estratégias suportadas: ``noFastForward``, ``squash``, ``rebase``,
    ``rebaseMerge``.

    Lança ``ProviderError`` se o merge falhar (conflitos, policies não
    satisfeitas, etc.).
    """
    pr = get_pull_request(org, project, repo, pr_number)
    last_sha = (
        pr.get("lastMergeSourceCommit", {}).get("commitId", "")
    )
    payload: dict[str, Any] = {
        "status": "completed",
        "lastMergeSourceCommit": {"commitId": last_sha},
        "completionOptions": {
            "mergeStrategy": merge_strategy,
            "deleteSourceBranch": False,
        },
    }
    url = _pr_url(org, project, repo, str(pr_number))
    return _request("PATCH", url, body=payload)


def delete_branch(
    org: str,
    project: str,
    repo: str,
    branch: str,
) -> None:
    """Deleta um branch remoto.

    Silencioso se o branch não existir (404 é ignorado).
    """
    # Precisamos do objectId atual do ref para deletar
    refs_list_url = (
        f"{org.rstrip('/')}/{project}/_apis/git/repositories/{repo}"
        f"/refs?filter=heads/{branch}&api-version={_API_VERSION}"
    )
    try:
        result = _request("GET", refs_list_url)
    except ProviderError:
        return  # Não existe — tudo bem

    refs = result.get("value", [])
    if not refs:
        return  # Branch não existe

    object_id = refs[0].get("objectId", "")
    payload = [
        {
            "name": f"refs/heads/{branch}",
            "oldObjectId": object_id,
            "newObjectId": "0000000000000000000000000000000000000000",
        }
    ]
    update_url = (
        f"{org.rstrip('/')}/{project}/_apis/git/repositories/{repo}"
        f"/refs?api-version={_API_VERSION}"
    )
    import contextlib
    with contextlib.suppress(ProviderError):
        _request("POST", update_url, body=payload)


def list_reviews(
    org: str,
    project: str,
    repo: str,
    pr_number: int,
) -> list:
    """Retorna os revisores e seus votos no PR.

    Cada item tem:
      - ``id``          : UUID do revisor
      - ``displayName`` : nome do revisor
      - ``vote``        : int (10=approved, 5=approved_with_suggestions,
                         0=no_vote, -5=waiting_for_author, -10=rejected)
      - ``isRequired``  : bool

    Normaliza para o mesmo formato que github_transport.get_pr_reviews:
      - ``state`` : ``APPROVED`` | ``CHANGES_REQUESTED`` | ``PENDING``
      - ``user``  : dict com ``login`` = displayName
    """
    url = (
        f"{org.rstrip('/')}/{project}/_apis/git/repositories/{repo}"
        f"/pullRequests/{pr_number}/reviewers?api-version={_API_VERSION}"
    )
    result = _request("GET", url)
    raw_reviewers = result.get("value", [])
    return [_normalize_reviewer(r) for r in raw_reviewers]


def post_comment(
    org: str,
    project: str,
    repo: str,
    pr_number: int,
    body: str,
) -> dict:
    """Posta um comentário (thread) no PR.

    Retorna o dict do thread criado.
    """
    payload: dict[str, Any] = {
        "comments": [{"content": body, "commentType": 1}],
        "status": 1,  # active
    }
    url = _threads_url(org, project, repo, pr_number)
    return _request("POST", url, body=payload)


def get_pr_for_branch(
    org: str,
    project: str,
    repo: str,
    branch: str,
) -> dict | None:
    """Retorna o PR ativo para o branch, ou None se não houver."""
    prs = list_pull_requests(org, project, repo, state="active", source_branch=branch)
    if not prs:
        return None
    return prs[0]


# ---------------------------------------------------------------------------
# Normalização interna
# ---------------------------------------------------------------------------

def _normalize_reviewer(raw: dict) -> dict:
    """Converte um revisor ADO para o formato canônico (compatível com GitHub)."""
    vote: int = raw.get("vote", 0)
    if vote >= 5:
        # 10 = approved, 5 = approved_with_suggestions — ambos contam como aprovação
        state = "APPROVED"
    elif vote <= -5:
        state = "CHANGES_REQUESTED"
    else:
        state = "PENDING"

    return {
        "id": raw.get("id", ""),
        "user": {"login": raw.get("displayName", "")},
        "state": state,
        "vote": vote,
        "isRequired": raw.get("isRequired", False),
        "submitted_at": None,  # ADO não expõe timestamp de voto diretamente
    }
