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
from typing import cast

from flow.ports.issue_provider import (
    ProviderError,
    ProviderPermissionError,
    ProviderSetupError,
)


def _run(args: list[str], timeout: int = 30) -> dict | list:  # type: ignore[type-arg]
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
    return cast(dict, _run([
        "issue", "view", str(number),
        "--repo", owner_repo,
        "--json", "number,title,labels,body,state,url,comments",
    ]))


def list_issues_by_label(owner_repo: str, label: str, limit: int = 50) -> list:
    """Lista issues abertas com a label dada."""
    return cast(list, _run([
        "issue", "list",
        "--repo", owner_repo,
        "--label", label,
        "--state", "open",
        "--json", "number,title,labels,url",
        "--limit", str(limit),
    ]))


def set_issue_labels(owner_repo: str, number: int, labels: list[str]) -> None:
    """Substitui todas as labels da issue via API do GitHub (PUT /issues/{n}/labels)."""
    # gh api --input lê JSON do stdin — única forma de mandar array sem serializar como string
    body = json.dumps({"labels": labels})
    result = subprocess.run(
        ["gh", "api", f"repos/{owner_repo}/issues/{number}/labels",
         "--method", "PUT", "--input", "-"],
        input=body,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ProviderError(f"gh label PUT falhou: {result.stderr.strip()}")


def get_issue_comments(owner_repo: str, number: int) -> list:
    """Retorna os comentários de uma issue."""
    return cast(list, _run([
        "api", f"repos/{owner_repo}/issues/{number}/comments",
        "--jq", ".",
    ]))


def create_issue_comment(owner_repo: str, number: int, body: str) -> dict:
    """Cria um comentário na issue."""
    return cast(dict, _run([
        "api", f"repos/{owner_repo}/issues/{number}/comments",
        "--method", "POST",
        "--field", f"body={body}",
    ]))


def update_issue_comment(owner_repo: str, comment_id: int, body: str) -> dict:
    """Atualiza um comentário existente."""
    return cast(dict, _run([
        "api", f"repos/{owner_repo}/issues/comments/{comment_id}",
        "--method", "PATCH",
        "--field", f"body={body}",
    ]))


def delete_issue_comment(owner_repo: str, comment_id: int) -> None:
    """Deleta um comentário de issue pelo ID.

    Silencioso se o comentário não existir (404 ignorado).
    """
    result = subprocess.run(
        ["gh", "api", f"repos/{owner_repo}/issues/comments/{comment_id}",
         "--method", "DELETE"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "404" in stderr or "Not Found" in stderr.lower():
            return  # já deletado — não é erro
        raise ProviderError(f"delete_issue_comment {comment_id}: {stderr}")


def get_pr_for_issue(owner_repo: str, issue_number: int) -> dict | None:
    """Retorna o PR aberto que fecha a issue dada, ou None se não existir.

    Usa a API de search para encontrar PR com 'Closes #N' no body.
    Retorna apenas PRs abertos (state=open) com head na branch feat/issue-N.
    """
    try:
        prs = cast(list, _run([
            "pr", "list",
            "--repo", owner_repo,
            "--state", "open",
            "--json", "number,title,headRefName,headRefOid,body,mergeable",
            "--limit", "50",
        ]))
    except ProviderError:
        return None

    needle = f"#{issue_number}"
    closes_patterns = [
        f"closes {needle}",
        f"closes: {needle}",
        f"close {needle}",
        f"fixes {needle}",
        f"resolves {needle}",
    ]
    for pr in prs:
        body_lower = (pr.get("body") or "").lower()
        if any(p in body_lower for p in closes_patterns):
            return cast(dict, pr)
    return None


def merge_pull_request(owner_repo: str, pr_number: int, merge_method: str = "squash") -> dict:
    """Faz o merge de um PR via GitHub API.

    ``merge_method`` = "squash" | "merge" | "rebase"
    Lança ``ProviderError`` se o merge falhar.
    """
    return cast(dict, _run([
        "api", f"repos/{owner_repo}/pulls/{pr_number}/merge",
        "--method", "PUT",
        "--field", f"merge_method={merge_method}",
    ]))


def get_pr_comments(owner_repo: str, pr_number: int) -> list:
    """Retorna os comentários (issue comments) de um PR.

    PRs compartilham a mesma thread de comentários que a issue no GitHub,
    acessível via ``/issues/{n}/comments``. Usa o número do PR diretamente.
    """
    return cast(list, _run([
        "api", f"repos/{owner_repo}/issues/{pr_number}/comments",
        "--jq", ".",
    ]))


def create_pr_comment(owner_repo: str, pr_number: int, body: str) -> dict:
    """Cria um comentário num PR."""
    return cast(dict, _run([
        "api", f"repos/{owner_repo}/issues/{pr_number}/comments",
        "--method", "POST",
        "--field", f"body={body}",
    ]))


def update_pr_comment(owner_repo: str, comment_id: int, body: str) -> dict:
    """Atualiza um comentário existente num PR."""
    return cast(dict, _run([
        "api", f"repos/{owner_repo}/issues/comments/{comment_id}",
        "--method", "PATCH",
        "--field", f"body={body}",
    ]))


def get_pr_checks(owner_repo: str, pr_number: int) -> list:
    """Retorna o status dos checks (CI) de um PR.

    Cada item tem: ``name``, ``state``, ``conclusion``.
    ``conclusion`` pode ser: success, failure, error, cancelled, skipped, neutral, None (pending).
    ``state`` pode ser: success, failure, error, pending, expected.

    Retorna lista vazia se não há checks configurados.
    Lança ``ProviderError`` em caso de falha de acesso.
    """
    try:
        return cast(list, _run([
            "pr", "checks", str(pr_number),
            "--repo", owner_repo,
            "--json", "name,state,conclusion",
        ]))
    except ProviderError:
        # gh pr checks retorna erro quando não há checks — tratar como lista vazia
        return []


def delete_branch(owner_repo: str, branch: str) -> None:
    """Deleta um branch remoto via GitHub API.

    Silencioso se o branch não existir (404 é ignorado).
    """
    result = subprocess.run(
        ["gh", "api", f"repos/{owner_repo}/git/refs/heads/{branch}",
         "--method", "DELETE"],
        capture_output=True,
        check=False,
    )
    # 404 = branch já deletado ou não existe — não é erro
    if result.returncode != 0 and b"404" not in result.stderr and b"Not Found" not in result.stderr:
        raise ProviderError(f"delete_branch {branch}: {result.stderr.decode()[:200]}")


def get_branch_exists(owner_repo: str, branch: str) -> bool:
    """Verifica se um branch existe no repositório.

    Retorna True se o branch existir, False caso contrário.
    Silencioso em erros — retorna False.
    """
    result = subprocess.run(
        ["gh", "api", f"repos/{owner_repo}/git/refs/heads/{branch}"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    if b"404" in result.stderr or b"Not Found" in result.stderr:
        return False
    # Outros erros: tratar como não existente (fail-safe)
    return False


def get_pr_reviews(owner_repo: str, pr_number: int) -> list:
    """Retorna os reviews de um PR.

    Cada item tem: ``id``, ``user`` (dict com ``login``), ``state``, ``submitted_at``.
    ``state`` pode ser: ``APPROVED``, ``CHANGES_REQUESTED``, ``COMMENTED``,
    ``DISMISSED``, ``PENDING``.

    Retorna lista vazia em caso de falha.
    """
    try:
        return cast(list, _run([
            "api", f"repos/{owner_repo}/pulls/{pr_number}/reviews",
        ]))
    except ProviderError:
        return []
