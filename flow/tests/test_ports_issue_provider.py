"""Testes de flow/ports/issue_provider.py.

Testa o contrato da porta, o dispatch e os erros normalizados.
NÃO testa a implementação dos adapters (isso é responsabilidade de #18, #19, #20).
"""

import pytest

from flow.ports.issue_provider import (
    IssueProvider,
    PROVIDERS,
    ProviderError,
    ProviderNotFoundError,
    ProviderPermissionError,
    ProviderSetupError,
    provider_for,
)


# ---------------------------------------------------------------------------
# Constantes e metadados da porta
# ---------------------------------------------------------------------------

class TestPortaMetadados:
    def test_providers_registrados(self) -> None:
        assert "github" in PROVIDERS
        assert "jira" in PROVIDERS

    def test_providers_e_tuple(self) -> None:
        assert isinstance(PROVIDERS, tuple)

    def test_erros_sao_subclasse_de_provider_error(self) -> None:
        assert issubclass(ProviderSetupError, ProviderError)
        assert issubclass(ProviderPermissionError, ProviderError)
        assert issubclass(ProviderNotFoundError, ProviderError)

    def test_erros_sao_subclasse_de_exception(self) -> None:
        assert issubclass(ProviderError, Exception)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class TestIssueProviderProtocol:
    def test_protocol_tem_todos_os_metodos(self) -> None:
        metodos_esperados = [
            "get_work_item",
            "list_by_state",
            "list_changed_since",
            "set_labels",
            "upsert_state_comment",
            "get_state_comment",
        ]
        for m in metodos_esperados:
            assert hasattr(IssueProvider, m), f"Protocol não tem {m!r}"

    def test_modulo_github_client_satisfaz_o_protocol_estruturalmente(self) -> None:
        """Os stubs já devem ter todos os métodos, mesmo retornando NotImplementedError."""
        from flow.adapters import github_client
        for m in IssueProvider.__protocol_attrs__:
            assert hasattr(github_client, m), (
                f"github_client não tem {m!r} — "
                f"adicione o stub antes de implementar (#18)"
            )

    def test_modulo_jira_client_satisfaz_o_protocol_estruturalmente(self) -> None:
        from flow.adapters import jira_client
        for m in IssueProvider.__protocol_attrs__:
            assert hasattr(jira_client, m), (
                f"jira_client não tem {m!r} — "
                f"adicione o stub antes de implementar (#19)"
            )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_provider_for_github_retorna_github_client(self) -> None:
        from flow.adapters import github_client
        assert provider_for("github") is github_client

    def test_provider_for_jira_retorna_jira_client(self) -> None:
        from flow.adapters import jira_client
        assert provider_for("jira") is jira_client

    def test_provider_desconhecido_cai_no_github(self) -> None:
        """Fallback para GitHub — degrada para chamada que falhará com erro claro."""
        from flow.adapters import github_client
        assert provider_for("azure") is github_client
        assert provider_for("") is github_client
        assert provider_for("qualquer-coisa") is github_client

    def test_dispatch_e_deterministico(self) -> None:
        """Chamar duas vezes com o mesmo argumento retorna o mesmo módulo."""
        assert provider_for("github") is provider_for("github")
        assert provider_for("jira") is provider_for("jira")
