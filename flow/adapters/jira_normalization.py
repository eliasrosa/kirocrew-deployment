"""Normalização Jira → contrato canônico do KiroCrew Flow.

Responsabilidade: mapear o formato cru do Jira para o dict normalizado.
Todas as peculiaridades do VGAT ficam aqui — nenhuma vaza para o client.

Peculiaridades documentadas (squad-jira.md):
  • "Débito Técnico" tem acento obrigatório no tipo
  • Task NÃO funciona no VGAT — usamos Enabler
  • Subtarefa e Sub-bug têm campo parent explícito
  • Labels são strings livres — o prefixo crewflow: funciona normalmente
  • Comentários usam ADF (Atlassian Document Format) no body.value,
    mas também têm body (plain) dependendo da versão da API
"""

from __future__ import annotations

import hashlib

# Marcador do comentário de estado (mesmo que o GitHub)
STATE_COMMENT_MARKER = "<!-- KIRO-FLOW-STATE -->"
STATE_COMMENT_CLOSE  = "<!-- /KIRO-FLOW-STATE -->"

# Tipos de issue que têm parent no VGAT
_CHILD_ISSUE_TYPES = frozenset({"Subtarefa", "Sub-bug", "Sub-teste"})


def normalize_item(raw: dict) -> dict:
    """Converte o payload cru de uma issue do Jira para o contrato canônico.

    Campos garantidos no retorno:
      key          : str  — "VGAT-123"
      title        : str
      labels       : list[str]
      state_comment: str | None
      parent_key   : str | None
      url          : str
    """
    fields = raw.get("fields") or {}
    key = raw.get("key", "")

    labels: list[str] = fields.get("labels", [])

    # Parent: Subtarefa e Sub-bug têm campo parent explícito
    issue_type = (fields.get("issuetype") or {}).get("name", "")
    parent_key: str | None = None
    if issue_type in _CHILD_ISSUE_TYPES:
        parent = fields.get("parent") or {}
        parent_key = parent.get("key")

    # Extrai o comentário de estado
    state_comment: str | None = _extract_state_comment(fields)

    # URL canônica — derivada da chave se não houver campo self
    self_url: str = raw.get("self", "")
    if self_url:
        # Converte API URL para URL de interface do usuário
        # https://your-org.atlassian.net/rest/api/2/issue/VGAT-123
        # → https://your-org.atlassian.net/browse/VGAT-123
        url = self_url.split("/rest/")[0] + f"/browse/{key}" if "/rest/" in self_url else self_url
    else:
        url = ""

    return {
        "key": key,
        "title": fields.get("summary", ""),
        "labels": labels,
        "state_comment": state_comment,
        "parent_key": parent_key,
        "url": url,
        "_raw": raw,
    }


def normalize_items_from_search(search_result: dict) -> list[dict]:
    """Normaliza o resultado de um /search do Jira."""
    return [normalize_item(issue) for issue in search_result.get("issues", [])]


def labels_hash(labels: list[str]) -> str:
    """Hash determinístico de um conjunto de labels (para o cache do scan).

    Idêntico ao do GitHub — sort garante independência de ordem.
    """
    canonical = ",".join(sorted(labels))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def extract_justification_from_state_comment(comment_body: str | None) -> str | None:
    """Extrai a justificativa de bypass do comentário de estado.

    Idêntico ao do GitHub — o formato do comentário é o mesmo.
    """
    if not comment_body or STATE_COMMENT_MARKER not in comment_body:
        return None

    for line in comment_body.splitlines():
        if "crewflow:hml-bypass" in line and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                justification = parts[2].strip()
                if justification:
                    return justification
    return None


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _extract_state_comment(fields: dict) -> str | None:
    """Extrai o comentário de estado dos comentários da issue."""
    comments_data = fields.get("comment") or {}
    comments = comments_data.get("comments", [])

    for comment in comments:
        # Tenta body plain primeiro, depois ADF rendered
        body = comment.get("body", "")
        if isinstance(body, dict):
            # ADF — tenta extrair texto plain dos conteúdos
            body = _adf_to_plain(body)
        if STATE_COMMENT_MARKER in body:
            return body
    return None


def _adf_to_plain(adf: dict) -> str:
    """Conversão mínima de ADF para texto plain.

    Suficiente para detectar o marcador <!-- KIRO-FLOW-STATE -->.
    Não é uma conversão completa de ADF.
    """
    texts: list[str] = []

    def _walk(node: dict | list) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node.get("text", ""))
            for child in node.get("content", []):
                _walk(child)

    _walk(adf)
    return " ".join(texts)
