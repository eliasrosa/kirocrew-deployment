"""Teste de paridade entre adapters — o gate que torna o dispatch seguro.

Um módulo não pode ser verificado estaticamente contra um Protocol.
Como os adapters são módulos (não classes), nada impede que jira_client e
github_client divirjam de assinatura — e a divergência só aparece em
runtime, para o usuário do provedor menos exercitado.

Este teste é o gate real; o Protocol em ports/ é o que ele checa contra.

Duas decisões de design copiadas do issue_radar interno do Kiro Crew:

1. GITHUB é a REFERÊNCIA, não um par entre iguais.
   O dispatch foi escrito contra o GitHub first. Uma discordância é sempre o
   OUTRO adapter drifting. Comparar cada um contra o GitHub (não pairwise)
   faz o teste nomear quem divergiu, não só "os dois não concordam".

2. O teste se AUTO-PROTEGE.
   test_a_tabela_cobre_todos_os_providers_registrados confere a tabela do
   próprio teste contra PROVIDERS da porta. Sem isso: alguém adiciona um
   terceiro adapter ao _PROVIDERS, não toca neste arquivo, o gate continua
   verde — e o adapter novo nunca é verificado.
"""

import inspect
import unittest

from flow.adapters import github_client, jira_client
from flow.ports.issue_provider import IssueProvider, PROVIDERS


class TestProviderParity(unittest.TestCase):
    """Todos os adapters registrados devem expor a mesma superfície."""

    REFERENCE = "github"

    CLIENTS: dict[str, object] = {
        "github": github_client,
        "jira": jira_client,
    }

    @property
    def others(self) -> list[str]:
        return sorted(set(self.CLIENTS) - {self.REFERENCE})

    # ------------------------------------------------------------------
    # Auto-guard
    # ------------------------------------------------------------------

    def test_a_tabela_cobre_todos_os_providers_registrados(self) -> None:
        """Um provider em PROVIDERS TEM que estar na tabela deste teste.

        Sem este teste, adicionar "azure" ao dispatch e esquecer de
        registrá-lo aqui deixaria o gate passando sem verificar o adapter.
        """
        tabela = set(self.CLIENTS)
        registrados = set(PROVIDERS)
        diff = registrados - tabela
        self.assertFalse(
            diff,
            f"\n\nProviders registrados em PROVIDERS mas AUSENTES da tabela deste teste:\n"
            f"  {sorted(diff)!r}\n\n"
            f"Adicione-os à classe TestProviderParity.CLIENTS antes de continuar.\n"
            f"Sem isso, o gate passa mesmo que o adapter novo não satisfaça a porta.",
        )

    def test_tabela_nao_tem_providers_fantasmas(self) -> None:
        """A tabela não pode ter providers que não estão em PROVIDERS."""
        tabela = set(self.CLIENTS)
        registrados = set(PROVIDERS)
        fantasmas = tabela - registrados
        self.assertFalse(
            fantasmas,
            f"Providers na tabela mas não registrados em PROVIDERS: {sorted(fantasmas)!r}",
        )

    # ------------------------------------------------------------------
    # Superfície completa
    # ------------------------------------------------------------------

    def test_todo_adapter_tem_todos_os_metodos_da_porta(self) -> None:
        """Cada adapter deve ter todos os métodos declarados no Protocol."""
        metodos = list(IssueProvider.__protocol_attrs__)

        for nome_provider, client in self.CLIENTS.items():
            for metodo in metodos:
                self.assertTrue(
                    hasattr(client, metodo),
                    f"\n\nAdapter {nome_provider!r} está faltando o método {metodo!r}.\n"
                    f"O dispatch vai quebrar em runtime para usuários deste provider.",
                )

    # ------------------------------------------------------------------
    # Assinaturas — o que os outros devem bater contra o GitHub
    # ------------------------------------------------------------------

    def test_assinaturas_batem_com_a_referencia(self) -> None:
        """Parâmetros de cada método devem bater com o GitHub (a referência).

        Inclui: nome, posição e valor default dos parâmetros.
        Exclui: o parâmetro ``self`` (módulos não têm).
        """
        ref = self.CLIENTS[self.REFERENCE]
        ref_metodos: dict[str, inspect.Signature] = {}

        for attr in IssueProvider.__protocol_attrs__:
            fn = getattr(ref, attr, None)
            if callable(fn):
                ref_metodos[attr] = inspect.signature(fn)

        for nome_provider in self.others:
            client = self.CLIENTS[nome_provider]
            for metodo, ref_sig in ref_metodos.items():
                fn = getattr(client, metodo, None)
                if not callable(fn):
                    continue  # já capturado em test_todo_adapter_tem_todos_os_metodos

                client_sig = inspect.signature(fn)
                ref_params = list(ref_sig.parameters.items())
                cli_params = list(client_sig.parameters.items())

                self.assertEqual(
                    len(ref_params),
                    len(cli_params),
                    f"\n\nMétodo {metodo!r}: {nome_provider!r} tem "
                    f"{len(cli_params)} parâmetros, referência (github) tem "
                    f"{len(ref_params)}.\n"
                    f"  referência : {list(ref_sig.parameters)}\n"
                    f"  {nome_provider}: {list(client_sig.parameters)}",
                )

                for (r_name, r_param), (c_name, c_param) in zip(ref_params, cli_params):
                    self.assertEqual(
                        r_name,
                        c_name,
                        f"\n\nMétodo {metodo!r}, posição {r_name!r}: "
                        f"adapter {nome_provider!r} tem parâmetro {c_name!r}, "
                        f"referência tem {r_name!r}.\n"
                        f"A ordem dos parâmetros importa para chamadas posicionais.",
                    )

    def test_nenhum_adapter_tem_metodo_publico_extra(self) -> None:
        """Adapters não devem expor métodos públicos que a referência não tem.

        Um método público extra sugere comportamento que o motor não conhece
        e pode indicar drift da porta.
        """
        ref = self.CLIENTS[self.REFERENCE]
        porta_metodos = set(IssueProvider.__protocol_attrs__)

        ref_publicos = {
            name for name in dir(ref)
            if not name.startswith("_") and callable(getattr(ref, name))
        }

        for nome_provider in self.others:
            client = self.CLIENTS[nome_provider]
            cli_publicos = {
                name for name in dir(client)
                if not name.startswith("_") and callable(getattr(client, name))
            }
            extra = cli_publicos - ref_publicos
            # Filtra o que pode ser importado de outros módulos
            extra_proprios = {
                m for m in extra
                if getattr(client, m, None) is not getattr(ref, m, None)
                and m not in porta_metodos
            }
            self.assertFalse(
                extra_proprios,
                f"\n\nAdapter {nome_provider!r} tem métodos públicos que a "
                f"referência (github) não tem:\n  {sorted(extra_proprios)!r}\n"
                f"Se esses métodos são necessários, adicione-os à porta (#17).",
            )


if __name__ == "__main__":
    unittest.main()
