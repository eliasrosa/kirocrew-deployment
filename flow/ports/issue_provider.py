"""Porta do KiroCrew Flow — o contrato que Jira e GitHub satisfazem.

DECISÃO DE DESIGN: adapters são MÓDULOS, não classes.

O dispatch é um dict de módulos:

    _PROVIDERS = {"github": github_client, "jira": jira_client}
    provider_for(squad) → o módulo correto

Isso significa que a conformidade não pode ser verificada estaticamente pelo
type checker — um módulo não pode ser registrado como instância de um Protocol.
O gate que verifica isso em runtime é ``tests/test_provider_parity.py`` (#20).

Por que módulos em vez de classes?
  • Não há estado a manter entre chamadas
  • Cada módulo é independentemente legível e testável
  • O dispatch é um dict literal — sem DI framework, sem magia

IDENTIDADE: ``project`` na assinatura, NUNCA ``repo``.

A chave primária do item de trabalho é projeto + issue key (ex: "VGAT-123").
O repo é derivado do título ([repo] Descrição) pelo domain/gates.py.
O Jira é projeto-cêntrico por natureza; o GitHub é repo-cêntrico mas
a porta normaliza para projeto para que o domínio seja agnóstico.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Tipos normalizados (o contrato canônico entre adapters e domínio)
# ---------------------------------------------------------------------------

class NormalizedItem:
    """Item de trabalho normalizado — independente do provedor.

    Todos os adapters devem retornar este formato.
    Usar dict simples por ora; converter para dataclass quando o esquema
    estabilizar.
    """

    # Campos obrigatórios que todo adapter deve preencher:
    # key: str        — "VGAT-123" ou "org/repo#42"
    # title: str      — texto do título
    # labels: list[str] — labels/tags atuais
    # state_comment: str | None — conteúdo do <!-- KIRO-FLOW-STATE -->, se houver
    # parent_key: str | None    — para subtarefa/sub-bug


class ProviderError(Exception):
    """Erro de provedor — sempre normalizado, nunca expõe detalhes do CLI."""


class ProviderSetupError(ProviderError):
    """Provedor não configurado (ex: `gh` não autenticado)."""


class ProviderPermissionError(ProviderError):
    """Sem permissão no projeto/repositório."""


class ProviderNotFoundError(ProviderError):
    """Item não encontrado."""


# ---------------------------------------------------------------------------
# O Protocol — a superfície que Jira e GitHub satisfazem
# ---------------------------------------------------------------------------

@runtime_checkable
class IssueProvider(Protocol):
    """Porta de acesso a sistemas de issue tracking.

    ``project`` identifica o projeto/organização:
      - Jira: chave do projeto (ex: "VGAT")
      - GitHub: "owner/repo" (ex: "eliasrosa/kirocrew-flow")

    Todos os métodos retornam dados normalizados — nenhum payload
    cru do provedor deve vazar para o domínio.
    """

    def get_work_item(self, project: str, key: str) -> dict:
        """Retorna o item normalizado ou lança ProviderNotFoundError."""
        ...

    def list_by_state(self, project: str, state: str) -> list[dict]:
        """Lista itens com a label de estado dada.

        ``state`` é o valor de um ``State`` (ex: "crewflow:todo").
        Retorna lista vazia se nenhum item corresponder.
        """
        ...

    def list_changed_since(self, project: str, since_hash: str) -> list[dict]:
        """Lista itens cujas labels mudaram desde o hash dado.

        O hash é o que o scan armazena no cache — compare o hash atual
        das labels com o armazenado para detectar mudança.
        Retorna lista vazia se nada mudou.
        """
        ...

    def set_labels(self, project: str, key: str, labels: list[str]) -> None:
        """Substitui as labels do item pelas dadas.

        Operação idempotente: chamar duas vezes com os mesmos argumentos
        não deve ter efeito colateral.
        """
        ...

    def upsert_state_comment(self, project: str, key: str, body: str) -> None:
        """Cria ou atualiza o comentário <!-- KIRO-FLOW-STATE --> do item.

        Deve substituir um comentário existente com o marcador em vez de
        criar um novo — "update in-place" para não poluir a thread.
        """
        ...

    def get_state_comment(self, project: str, key: str) -> str | None:
        """Retorna o conteúdo do comentário <!-- KIRO-FLOW-STATE --> ou None."""
        ...


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

# Provedores registrados — mapeamento nome → módulo adapter.
# Para adicionar um terceiro provedor: adicione aqui E em test_provider_parity.py.
# O teste de paridade (#20) garante que todo provedor registrado satisfaz a porta.
PROVIDERS: tuple[str, ...] = ("github", "jira")

_DEFAULT_PROVIDER = "github"


def _build_dispatch() -> dict[str, IssueProvider]:
    """Constrói o dict de dispatch com import lazy para evitar dependências circulares."""
    from flow.adapters import github_client, jira_client

    return {
        "github": github_client,  # type: ignore[dict-item]
        "jira": jira_client,      # type: ignore[dict-item]
    }


_dispatch: dict[str, IssueProvider] | None = None


def provider_for(issue_provider: str) -> IssueProvider:
    """Retorna o módulo adapter para o provedor dado.

    ``issue_provider`` é o campo ``issue_provider`` do schema da squad
    (ex: "github" ou "jira").

    Um provedor desconhecido cai silenciosamente no GitHub — isso não é
    um bug: degrada para uma chamada GitHub que falhará com erro claro,
    em vez de lançar uma exceção misteriosa na camada de routing.
    """
    if _cache["dispatch"] is None:
        _cache["dispatch"] = _build_dispatch()
    dispatch: dict[str, IssueProvider] = _cache["dispatch"]  # type: ignore[assignment]
    return dispatch.get(issue_provider, dispatch[_DEFAULT_PROVIDER])


# Cache mutável num dict para evitar `global` statement (PLW0603).
_cache: dict[str, dict[str, IssueProvider] | None] = {"dispatch": None}
