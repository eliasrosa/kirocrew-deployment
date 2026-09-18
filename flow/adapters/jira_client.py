"""Adapter Jira — orquestração; satisfaz IssueProvider.

IDENTIDADE JIRA: ``project`` é a chave do projeto (ex: "VGAT").
O ``key`` da issue é sempre "PROJETO-NUMERO" (ex: "VGAT-123").

O scan usa JQL de projeto (não por repositório):
  project = VGAT AND labels = "crewflow:todo" AND statusCategory != Done
"""

from __future__ import annotations

from flow.adapters import jira_normalization as norm
from flow.adapters import jira_transport as transport
from flow.ports.issue_provider import ProviderNotFoundError


def get_work_item(project: str, key: str) -> dict:
    """Retorna a issue normalizada.

    ``project`` = chave do projeto (ex: "VGAT")
    ``key``     = chave da issue (ex: "VGAT-123")
    """
    raw = transport.get_issue(project, key)
    if not raw or "key" not in raw:
        raise ProviderNotFoundError(f"issue {key!r} não encontrada no projeto {project!r}")
    return norm.normalize_item(raw)


def list_by_state(project: str, state: str) -> list[dict]:
    """Lista issues com a label de estado dada via JQL de projeto.

    ``state`` = valor de um State, ex: "crewflow:todo"

    A query é por projeto (não por repo) — é a natureza do Jira.
    """
    result = transport.search_issues_by_label(project, label=state)
    return norm.normalize_items_from_search(result)


def list_changed_since(project: str, since_hash: str) -> list[dict]:
    """Lista issues cujas labels mudaram desde o hash armazenado.

    Estratégia simples: lista todas as issues com labels crewflow:* e
    filtra pelo hash.

    Para um scan de alta escala: usar JQL com ``updated >=`` e comparar
    só as issues atualizadas recentemente.
    """
    from flow.domain.state import State

    all_items: list[dict] = []
    seen_keys: set[str] = set()

    for state in State:
        result = transport.search_issues_by_label(project, label=state.value)
        for item in norm.normalize_items_from_search(result):
            k = item["key"]
            if k not in seen_keys:
                seen_keys.add(k)
                current_hash = norm.labels_hash(item["labels"])
                if current_hash != since_hash:
                    all_items.append(item)

    return all_items


def set_labels(project: str, key: str, labels: list[str]) -> None:
    transport.update_issue_labels(key, labels)


def upsert_state_comment(project: str, key: str, body: str) -> None:
    """Cria ou atualiza o comentário <!-- KIRO-FLOW-STATE --> da issue."""
    comments = transport.get_issue_comments(key)

    existing_id: str | None = None
    for comment in comments:
        comment_body = comment.get("body", "")
        if isinstance(comment_body, dict):
            from flow.adapters.jira_normalization import _adf_to_plain
            comment_body = _adf_to_plain(comment_body)
        if norm.STATE_COMMENT_MARKER in comment_body:
            existing_id = comment.get("id")
            break

    if existing_id is not None:
        transport.update_comment(key, existing_id, body)
    else:
        transport.add_issue_comment(key, body)


def get_state_comment(project: str, key: str) -> str | None:
    """Retorna o conteúdo do comentário de estado, ou None."""
    comments = transport.get_issue_comments(key)

    for comment in comments:
        comment_body = comment.get("body", "")
        if isinstance(comment_body, dict):
            from flow.adapters.jira_normalization import _adf_to_plain
            comment_body = _adf_to_plain(comment_body)
        if norm.STATE_COMMENT_MARKER in comment_body:
            return comment_body
    return None
