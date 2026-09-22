"""Adapter GitHub — orquestração; satisfaz IssueProvider.

Composição das três camadas:
  github_transport.py    — I/O via gh CLI (patchável nos testes)
  github_normalization.py — payload cru → contrato canônico
  este módulo            — lógica de orquestração

IDENTIDADE GITHUB: no GitHub a "issue" pertence a um repositório,
não a um projeto. A porta usa ``project`` para ser agnóstica de provedor,
então aqui ``project`` é sempre "owner/repo" (ex: "eliasrosa/kirocrew-flow").

O comentário de estado usa o marcador <!-- KIRO-FLOW-STATE --> / <!-- /KIRO-FLOW-STATE -->.
O método upsert_state_comment localiza e atualiza um comentário existente
para não poluir a thread de comentários com duplicatas.
"""

from __future__ import annotations

from flow.adapters import github_normalization as norm
from flow.adapters import github_transport as transport
from flow.audit.state_comment import PR_REVIEW_COMMENT_MARKER
from flow.ports.issue_provider import ProviderNotFoundError


def get_work_item(project: str, key: str) -> dict:
    """Retorna a issue normalizada.

    ``project`` = "owner/repo"
    ``key``     = número da issue como string (ex: "42") ou URL completa
    """
    # Extrai o número da issue de diferentes formatos
    number = _parse_issue_number(key)
    raw = transport.get_issue(project, number)
    if not raw:
        raise ProviderNotFoundError(f"issue #{number} não encontrada em {project!r}")
    return norm.normalize_item(raw)


def list_by_state(project: str, state: str) -> list[dict]:
    """Lista issues abertas com a label de estado dada.

    ``state`` = valor de um State, ex: "crewflow:todo"
    """
    raw_list = transport.list_issues_by_label(project, label=state)
    return norm.normalize_items(raw_list)


def list_changed_since(project: str, since_hash: str) -> list[dict]:
    """Lista issues cujas labels mudaram desde o hash armazenado.

    Estratégia: lista todas as issues com labels crewflow:* e filtra
    aquelas cujo hash atual difere do ``since_hash``.

    Para um scan de alta escala: use a API de events/timeline para
    verificar apenas issues com label_added/label_removed recentes.
    """

    from flow.domain.state import State

    all_items: list[dict] = []
    seen_keys: set[str] = set()

    for state in State:
        raw_list = transport.list_issues_by_label(project, label=state.value)
        for item in norm.normalize_items(raw_list):
            k = item["key"]
            if k not in seen_keys:
                seen_keys.add(k)
                current_hash = norm.labels_hash(item["labels"])
                if current_hash != since_hash:
                    all_items.append(item)

    return all_items


def set_labels(project: str, key: str, labels: list[str]) -> None:
    number = _parse_issue_number(key)
    transport.set_issue_labels(project, number, labels)


def edit_issue_labels(
    project: str,
    issue_number: int,
    add: list[str],
    remove: list[str],
) -> None:
    """Troca labels da issue de forma atômica via ``gh issue edit``.

    Executa a transição ``--add-label X --remove-label Y`` em um único
    comando do gh CLI. Isso é preferível a ``set_labels`` quando se quer
    mudar apenas um subconjunto de labels sem substituir toda a lista,
    pois evita race conditions entre crons concorrentes que poderiam
    sobrescrever labels adicionadas por outro processo.

    Lança ``ProviderError`` se o gh CLI falhar.
    """
    transport.edit_issue_labels(project, issue_number, add=add, remove=remove)


def upsert_state_comment(project: str, key: str, body: str) -> None:
    """Cria ou atualiza o comentário <!-- KIRO-FLOW-STATE --> da issue.

    Garante que exista exatamente UM comentário com o marcador:
    - 0 comentários com marker → cria
    - 1 comentário com marker → atualiza in-place
    - N > 1 comentários com marker (duplicatas acumuladas) → atualiza o
      primeiro e deleta os demais, restaurando o invariante de 1 único comentário
    """
    number = _parse_issue_number(key)
    comments = transport.get_issue_comments(project, number)

    # Coleta TODOS os comentários com o marcador (preserva a ordem de criação)
    marker_comments: list[dict] = [
        c for c in comments
        if norm.STATE_COMMENT_MARKER in c.get("body", "")
    ]

    if not marker_comments:
        # Nenhum existe ainda — cria
        transport.create_issue_comment(project, number, body)
        return

    # Atualiza o primeiro (o mais antigo, que fica como referência permanente)
    transport.update_issue_comment(project, marker_comments[0]["id"], body)

    # Remove duplicatas excedentes (resultado de ciclos anteriores que usavam
    # `gh issue comment` em vez de upsert)
    for duplicate in marker_comments[1:]:
        transport.delete_issue_comment(project, duplicate["id"])


def get_state_comment(project: str, key: str) -> str | None:
    """Retorna o conteúdo do comentário de estado, ou None.

    Retorna o PRIMEIRO comentário com o marcador (o mesmo que upsert_state_comment
    atualiza), garantindo consistência entre leitura e escrita.
    """
    number = _parse_issue_number(key)
    comments = transport.get_issue_comments(project, number)

    for comment in comments:
        body = comment.get("body", "")
        if norm.STATE_COMMENT_MARKER in body:
            return body
    return None


def get_pr_for_issue(project: str, issue_number: int) -> dict | None:
    """Retorna o PR aberto associado à issue, ou None se não encontrado."""
    return transport.get_pr_for_issue(project, issue_number)


def merge_pull_request(project: str, pr_number: int, merge_method: str = "squash") -> dict:
    """Faz o merge squash de um PR.

    Lança ``ProviderError`` se o merge falhar (ex: checks falhando, conflito).
    """
    return transport.merge_pull_request(project, pr_number, merge_method=merge_method)


def upsert_pr_review_comment(project: str, pr_number: int, body: str) -> None:
    """Cria ou atualiza o comentário <!-- KIRO-FLOW-REVIEW --> no PR.

    Segue o mesmo padrão de upsert_state_comment: evita poluir a thread
    com múltiplos comentários — atualiza in-place se já existir.
    """
    comments = transport.get_pr_comments(project, pr_number)
    existing_id: int | None = None
    for comment in comments:
        if PR_REVIEW_COMMENT_MARKER in comment.get("body", ""):
            existing_id = comment["id"]
            break

    if existing_id is not None:
        transport.update_pr_comment(project, existing_id, body)
    else:
        transport.create_pr_comment(project, pr_number, body)


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _parse_issue_number(key: str) -> int:
    """Extrai o número de issue de diferentes formatos.

    Aceita:
      "42"            → 42
      "#42"           → 42
      "owner/repo#42" → 42
      "https://github.com/.../issues/42" → 42
    """
    key = key.strip()
    if key.startswith("#"):
        return int(key[1:])
    if "/" in key or "https://" in key:
        # Extrai o número do final da string
        import re
        m = re.search(r"#(\d+)$|/(\d+)$", key)
        if m:
            return int(m.group(1) or m.group(2))
    return int(key)


def delete_branch(project: str, branch: str) -> None:
    """Deleta um branch remoto. Silencioso se não existir."""
    from flow.adapters import github_transport as _t
    _t.delete_branch(project, branch)


def get_pr_checks(project: str, pr_number: int) -> list:
    """Retorna os checks (CI) de um PR.

    Cada item tem: ``name``, ``state``, ``conclusion``.
    Retorna lista vazia se não há checks configurados ou erro de acesso.
    """
    from flow.adapters import github_transport as _t
    return _t.get_pr_checks(project, pr_number)


def add_issue_comment(project: str, issue_number: int, body: str) -> dict:
    """Adiciona um comentário a uma issue."""
    from flow.adapters import github_transport as _t
    return _t.create_issue_comment(project, issue_number, body)


def get_branch_exists(project: str, branch: str) -> bool:
    """Verifica se um branch existe no repositório.

    Retorna True se o branch existir, False caso contrário.
    """
    from flow.adapters import github_transport as _t
    return _t.get_branch_exists(project, branch)


def get_pr_reviews(project: str, pr_number: int) -> list:
    """Retorna os reviews de um PR.

    Cada item tem: ``id``, ``user``, ``state``, ``submitted_at``.
    ``state`` pode ser: ``APPROVED``, ``CHANGES_REQUESTED``, ``COMMENTED``,
    ``DISMISSED``, ``PENDING``.

    Retorna lista vazia em caso de falha.
    """
    from flow.adapters import github_transport as _t
    return _t.get_pr_reviews(project, pr_number)
