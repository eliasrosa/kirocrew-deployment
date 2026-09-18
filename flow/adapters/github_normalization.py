"""Normalização GitHub → contrato canônico do KiroCrew Flow.

Responsabilidade única: mapear o formato cru do GitHub para o dict
normalizado que o domínio e a porta esperam.

Esta camada isola o formato do provedor do resto do sistema. Se o GitHub
mudar o formato de um campo, só esta camada muda.
"""

from __future__ import annotations

# Marcador do comentário de estado
STATE_COMMENT_MARKER = "<!-- KIRO-FLOW-STATE -->"
STATE_COMMENT_CLOSE  = "<!-- /KIRO-FLOW-STATE -->"


def normalize_item(raw: dict) -> dict:
    """Converte o payload cru de uma issue do GitHub para o contrato canônico.

    Campos garantidos no retorno:
      key          : str  — "owner/repo#NUMBER"
      title        : str
      labels       : list[str]
      state_comment: str | None
      parent_key   : str | None
      url          : str
    """
    labels = [lbl["name"] for lbl in raw.get("labels", [])]

    # Extrai o comentário de estado dos comentários da issue, se carregados
    state_comment: str | None = None
    for comment in raw.get("comments", []):
        body = comment.get("body", "")
        if STATE_COMMENT_MARKER in body:
            state_comment = body
            break

    return {
        "key": raw.get("url", ""),       # URL é o identificador canônico no GitHub
        "title": raw.get("title", ""),
        "labels": labels,
        "state_comment": state_comment,
        "parent_key": None,               # GitHub não tem hierarquia nativa de issues
        "url": raw.get("url", ""),
        "_raw": raw,                      # preservado para debug; não usar no domínio
    }


def normalize_items(raw_list: list[dict]) -> list[dict]:
    """Normaliza uma lista de issues."""
    return [normalize_item(item) for item in raw_list]


def labels_hash(labels: list[str]) -> str:
    """Hash determinístico de um conjunto de labels (para o cache do scan).

    A ordem das labels não importa — sort garante que dois conjuntos
    iguais produzam o mesmo hash.
    """
    import hashlib
    canonical = ",".join(sorted(labels))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def extract_justification_from_state_comment(comment_body: str | None) -> str | None:
    """Extrai a justificativa de bypass do comentário de estado.

    Procura por um campo "| `crewflow:hml-bypass` | <justificativa> |" na
    tabela de exceções do comentário estruturado.

    Retorna None se não encontrar nenhuma justificativa.
    """
    if not comment_body or STATE_COMMENT_MARKER not in comment_body:
        return None

    # Procura pela linha da exceção de bypass
    for line in comment_body.splitlines():
        if "crewflow:hml-bypass" in line and "|" in line:
            parts = [p.strip() for p in line.split("|")]
            # Formato: | `crewflow:hml-bypass` | justificativa | quem | quando |
            if len(parts) >= 3:
                justification = parts[2].strip()
                if justification:
                    return justification
    return None
