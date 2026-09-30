"""Transport do Jira — fachada patchável sobre o MCP jira::*.

TODO único desta camada: executar chamadas MCP e retornar o JSON cru.

Por que MCP e não REST direto?
  • O MCP ``jira_get_issue`` já resolve credenciais e host
  • A tool ``jira_update_issue`` trata ADF e os quirks do VGAT
  • Manter aqui facilita mockar nos testes sem levantar servidor real

Erros são normalizados para ProviderError antes de sair deste módulo.
"""

from __future__ import annotations

from flow.ports.issue_provider import (
    ProviderError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderSetupError,
)

# ---------------------------------------------------------------------------
# Interface MCP (injetável nos testes)
# ---------------------------------------------------------------------------
# Em produção, o MCP é chamado pelo executor (deployment.py) que tem
# acesso às tools MCP. No scan zero-token, o transport usa a API REST
# do Jira via requests. Para testes, o transport inteiro é patchado.
#
# Por simplicidade da Fase 1: os métodos abaixo funcionam como wrappers
# que o executor chama após obter os dados via MCP. O transport do scan
# (zero-token) usa requests; o do executor usa as tools MCP.
# Fase 2 refinará a separação.

def get_issue(project: str, key: str) -> dict:
    """Retorna o payload cru de uma issue do Jira.

    Internamente usa a API REST v2 (compatível com Cloud e Server).
    Em produção, credenciais via variável de ambiente JIRA_API_TOKEN / JIRA_BASE_URL.
    """
    return _rest_get(f"/rest/api/2/issue/{key}")


def search_issues_by_label(project: str, label: str, max_results: int = 50) -> dict:
    """Busca issues com a label dada usando JQL.

    Retorna o payload cru do endpoint /rest/api/2/search.
    """
    jql = f'project = {project} AND labels = "{label}" AND statusCategory != Done'
    return _rest_get(
        "/rest/api/2/search",
        params={
            "jql": jql,
            "maxResults": max_results,
            "fields": "summary,labels,status,comment,parent,issuetype",
        },
    )


def get_subtasks(key: str) -> list[dict]:
    """Retorna as sub-tasks nativas de uma issue do Jira.

    Lê o campo ``fields.subtasks`` do issue pai — cada item cru traz
    ``key``, ``fields.summary`` e ``fields.status.statusCategory.key``
    ("done" quando resolvida). Retorna lista vazia se não houver sub-tasks.
    """
    raw = _rest_get(
        f"/rest/api/2/issue/{key}",
        params={"fields": "subtasks"},
    )
    fields = raw.get("fields") or {}
    return fields.get("subtasks") or []


def update_issue_labels(key: str, labels: list[str]) -> None:
    """Substitui as labels da issue."""
    _rest_put(f"/rest/api/2/issue/{key}", body={"fields": {"labels": labels}})


def get_issue_comments(key: str) -> list[dict]:
    """Retorna os comentários da issue."""
    data = _rest_get(f"/rest/api/2/issue/{key}/comment")
    return data.get("comments", [])


def add_issue_comment(key: str, body_text: str) -> dict:
    """Adiciona um comentário à issue."""
    return _rest_post(
        f"/rest/api/2/issue/{key}/comment",
        body={"body": body_text},
    )


def update_comment(key: str, comment_id: str, body_text: str) -> dict:
    """Atualiza um comentário existente."""
    return _rest_put(
        f"/rest/api/2/issue/{key}/comment/{comment_id}",
        body={"body": body_text},
    )


# ---------------------------------------------------------------------------
# REST helper (patchável nos testes)
# ---------------------------------------------------------------------------

def _get_config() -> tuple[str, dict[str, str]]:
    """Retorna (base_url, headers) a partir de variáveis de ambiente.

    Variáveis esperadas:
      JIRA_BASE_URL   — ex: "https://cogna.atlassian.net"
      JIRA_API_TOKEN  — token de API Jira (email:token em base64, ou só token Cloud)
      JIRA_EMAIL      — email do usuário (necessário para Cloud)
    """
    import base64
    import os

    base_url = os.environ.get("JIRA_BASE_URL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    email = os.environ.get("JIRA_EMAIL", "")

    if not base_url or not token:
        raise ProviderSetupError(
            "Jira não configurado — defina JIRA_BASE_URL e JIRA_API_TOKEN"
        )

    if email:
        creds = base64.b64encode(f"{email}:{token}".encode()).decode()
        auth_header = f"Basic {creds}"
    else:
        auth_header = f"Bearer {token}"

    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return base_url.rstrip("/"), headers


def _rest_get(path: str, params: dict | None = None) -> dict:
    import urllib.parse
    import urllib.request

    base_url, headers = _get_config()
    url = base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    return _http_request("GET", url, headers=headers)


def _rest_post(path: str, body: dict) -> dict:
    import json

    base_url, headers = _get_config()
    url = base_url + path
    return _http_request("POST", url, headers=headers, body=json.dumps(body).encode())


def _rest_put(path: str, body: dict) -> dict:
    import json

    base_url, headers = _get_config()
    url = base_url + path
    return _http_request("PUT", url, headers=headers, body=json.dumps(body).encode())


def _http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None = None,
) -> dict:
    import json
    import urllib.request

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read().decode()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        content = exc.read().decode()
        if exc.code == 401:
            raise ProviderSetupError(f"Jira: não autenticado (401). {content}") from exc
        if exc.code == 403:
            raise ProviderPermissionError(f"Jira: sem permissão (403). {content}") from exc
        if exc.code == 404:
            raise ProviderNotFoundError(f"Jira: não encontrado (404). {url}") from exc
        raise ProviderError(f"Jira HTTP {exc.code}: {content}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Jira: erro de rede — {exc.reason}") from exc
