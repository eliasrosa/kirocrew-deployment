"""GithubTransport — re-exporta as funções de ``flow.adapters.github_transport``.

Este módulo existe para que o ``ScmTransportFactory`` possa referenciar
o GitHub e o Azure DevOps de forma uniforme, sem duplicar código.

Toda a implementação real fica em ``flow/adapters/github_transport.py``
(o módulo legado); aqui apenas re-exportamos o que a factory precisa.
"""

from __future__ import annotations

from flow.adapters.github_transport import (
    create_pr_comment,
    delete_branch,
    get_branch_exists,
    get_pr_checks,
    get_pr_for_issue,
    get_pr_reviews,
    merge_pull_request,
)

__all__ = [
    "create_pr_comment",
    "delete_branch",
    "get_branch_exists",
    "get_pr_checks",
    "get_pr_for_issue",
    "get_pr_reviews",
    "merge_pull_request",
]
