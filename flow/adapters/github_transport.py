"""Transport do GitHub — fachada patchável sobre o ``gh`` CLI.

TODO único desta camada: executar comandos ``gh`` e retornar o JSON cru.

A separação transport / normalization / client é o que torna os testes
unitários viáveis sem rede: os testes usam ``mock.patch.object`` aqui,
nunca interceptam HTTP.

Todos os erros são normalizados para ``ProviderError`` antes de sair
deste módulo — nenhum detalhe do ``gh`` vaza para o client.
"""

from __future__ import annotations

import json
import subprocess

from flow.ports.issue_provider import (
    ProviderError,
    ProviderPermissionError,
    ProviderSetupError,
)


def _run(args: list[str], timeout: int = 30) -> dict | list:
    """Executa um comando ``gh`` e retorna o JSON parseado.

    Lança ``ProviderSetupError`` se o ``gh`` não estiver autenticado.
    Lança ``ProviderPermissionError`` se o acesso for negado.
    Lança ``ProviderError`` para outros erros.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ProviderSetupError(
            "gh CLI não encontrado — instale e autentique com `gh auth login`"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(f"gh timeout após {timeout}s: {args[0]}") from exc

    stderr = result.stderr.strip()

    if result.returncode != 0:
        low = stderr.lower()
        if "not logged in" in low or "authentication" in low or "auth" in low:
            raise ProviderSetupError(
                f"gh não autenticado — execute `gh auth login`. Detalhe: {stderr}"
            )
        if "403" in stderr or "forbidden" in low or "permission" in low:
            raise ProviderPermissionError(
                f"sem permissão no repositório. Detalhe: {stderr}"
            )
        raise ProviderError(f"gh falhou (exit {result.returncode}): {stderr}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProviderError(
            f"gh retornou JSON inválido: {result.stdout[:200]!r}"
        ) from exc


def get_issue(owner_repo: str, number: int) -> dict:
    """Retorna o JSON cru de uma issue do GitHub."""
    return _run([
        "issue", "view", str(number),
        "--repo", owner_repo,
        "--json", "number,title,labels,body,state,url,comments",
    ])


def list_issues_by_label(owner_repo: str, label: str, limit: int = 50) -> list:
    """Lista issues abertas com a label dada."""
    return _run([
        "issue", "list",
        "--repo", owner_repo,
        "--label", label,
        "--state", "open",
        "--json", "number,title,labels,url",
        "--limit", str(limit),
    ])


def set_issue_labels(owner_repo: str, number: int, labels: list[str]) -> None:
    """Substitui todas as labels da issue."""
    # Remove todas as labels atuais e adiciona as novas.
    # gh issue edit aceita --add-label e --remove-label mas não "set".
    # A forma mais segura é usar a API diretamente via gh api.
    _run([
        "api", f"repos/{owner_repo}/issues/{number}/labels",
        "--method", "PUT",
        "--field", f"labels={json.dumps(labels)}",
    ])


def get_issue_comments(owner_repo: str, number: int) -> list:
    """Retorna os comentários de uma issue."""
    return _run([
        "api", f"repos/{owner_repo}/issues/{number}/comments",
        "--jq", ".",
    ])


def create_issue_comment(owner_repo: str, number: int, body: str) -> dict:
    """Cria um comentário na issue."""
    return _run([
        "api", f"repos/{owner_repo}/issues/{number}/comments",
        "--method", "POST",
        "--field", f"body={body}",
    ])


def update_issue_comment(owner_repo: str, comment_id: int, body: str) -> dict:
    """Atualiza um comentário existente."""
    return _run([
        "api", f"repos/{owner_repo}/issues/comments/{comment_id}",
        "--method", "PATCH",
        "--field", f"body={body}",
    ])
