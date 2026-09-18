"""Testes do adapter GitHub.

Todos sem rede — usam ``mock.patch.object`` no transport.
O transport é a fachada patchável: interceptamos lá, nunca no HTTP.
"""

from __future__ import annotations

from unittest import mock

import pytest

from flow.adapters import github_client, github_normalization as norm, github_transport
from flow.ports.issue_provider import ProviderNotFoundError, ProviderSetupError


# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _raw_issue(number: int = 42, title: str = "[api-gw2] Fix", labels: list[str] | None = None) -> dict:
    return {
        "number": number,
        "title": title,
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "state": "OPEN",
        "url": f"https://github.com/owner/repo/issues/{number}",
        "body": "",
        "comments": [],
    }


# ---------------------------------------------------------------------------
# github_normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_campos_obrigatorios_presentes(self) -> None:
        item = norm.normalize_item(_raw_issue())
        assert "key" in item
        assert "title" in item
        assert "labels" in item
        assert "state_comment" in item
        assert "parent_key" in item
        assert "url" in item

    def test_labels_extraidas_como_lista_de_strings(self) -> None:
        raw = _raw_issue(labels=["crewflow:todo", "phase-1"])
        item = norm.normalize_item(raw)
        assert item["labels"] == ["crewflow:todo", "phase-1"]

    def test_state_comment_extraido_do_primeiro_comentario_com_marcador(self) -> None:
        raw = _raw_issue()
        raw["comments"] = [
            {"body": "comentário normal"},
            {"body": f"{norm.STATE_COMMENT_MARKER}\nstatus: running\n{norm.STATE_COMMENT_CLOSE}"},
        ]
        item = norm.normalize_item(raw)
        assert item["state_comment"] is not None
        assert "status: running" in item["state_comment"]

    def test_state_comment_none_se_ausente(self) -> None:
        item = norm.normalize_item(_raw_issue())
        assert item["state_comment"] is None

    def test_parent_key_sempre_none_no_github(self) -> None:
        item = norm.normalize_item(_raw_issue())
        assert item["parent_key"] is None

    def test_labels_hash_e_deterministico(self) -> None:
        h1 = norm.labels_hash(["crewflow:todo", "phase-1"])
        h2 = norm.labels_hash(["phase-1", "crewflow:todo"])  # ordem diferente
        assert h1 == h2

    def test_labels_hash_difere_para_conjuntos_diferentes(self) -> None:
        h1 = norm.labels_hash(["crewflow:todo"])
        h2 = norm.labels_hash(["crewflow:dev"])
        assert h1 != h2

    def test_extract_justification_retorna_none_sem_comentario(self) -> None:
        assert norm.extract_justification_from_state_comment(None) is None
        assert norm.extract_justification_from_state_comment("") is None

    def test_extract_justification_encontra_bypass(self) -> None:
        body = (
            f"{norm.STATE_COMMENT_MARKER}\n"
            "### Exceções\n"
            "| Exceção | Justificativa | Quem | Quando |\n"
            "|---------|---------------|------|--------|\n"
            "| `crewflow:hml-bypass` | Checkout fora do ar | @elias | 2026-09-15 |\n"
            f"{norm.STATE_COMMENT_CLOSE}"
        )
        j = norm.extract_justification_from_state_comment(body)
        assert j == "Checkout fora do ar"


# ---------------------------------------------------------------------------
# github_transport — testes de tratamento de erro
# ---------------------------------------------------------------------------

class TestTransportErrors:
    def test_falha_de_autenticacao_lanca_setup_error(self) -> None:
        completed = mock.MagicMock()
        completed.returncode = 1
        completed.stderr = "not logged in"
        completed.stdout = ""

        with mock.patch("subprocess.run", return_value=completed):
            with pytest.raises(ProviderSetupError, match="autenticado"):
                github_transport._run(["issue", "view", "1"])

    def test_json_invalido_lanca_provider_error(self) -> None:
        from flow.ports.issue_provider import ProviderError
        completed = mock.MagicMock()
        completed.returncode = 0
        completed.stdout = "não é json"
        completed.stderr = ""

        with mock.patch("subprocess.run", return_value=completed):
            with pytest.raises(ProviderError, match="JSON inválido"):
                github_transport._run(["api", "anything"])


# ---------------------------------------------------------------------------
# github_client — testes de orquestração (mock no transport)
# ---------------------------------------------------------------------------

class TestGetWorkItem:
    def test_retorna_item_normalizado(self) -> None:
        raw = _raw_issue(42, "[api-gw2] Fix", ["crewflow:dev"])
        with mock.patch.object(github_transport, "get_issue", return_value=raw):
            item = github_client.get_work_item("owner/repo", "42")

        assert item["title"] == "[api-gw2] Fix"
        assert "crewflow:dev" in item["labels"]

    def test_aceita_chave_com_hash(self) -> None:
        raw = _raw_issue(99)
        with mock.patch.object(github_transport, "get_issue", return_value=raw) as m:
            github_client.get_work_item("owner/repo", "#99")
            m.assert_called_once_with("owner/repo", 99)

    def test_aceita_url_completa(self) -> None:
        raw = _raw_issue(7)
        with mock.patch.object(github_transport, "get_issue", return_value=raw) as m:
            github_client.get_work_item("owner/repo", "https://github.com/owner/repo/issues/7")
            m.assert_called_once_with("owner/repo", 7)

    def test_issue_vazia_lanca_not_found(self) -> None:
        with mock.patch.object(github_transport, "get_issue", return_value={}):
            with pytest.raises(ProviderNotFoundError):
                github_client.get_work_item("owner/repo", "999")


class TestListByState:
    def test_retorna_lista_normalizada(self) -> None:
        raw_list = [_raw_issue(1, "[gw] Fix 1", ["crewflow:todo"])]
        with mock.patch.object(github_transport, "list_issues_by_label", return_value=raw_list):
            items = github_client.list_by_state("owner/repo", "crewflow:todo")

        assert len(items) == 1
        assert "crewflow:todo" in items[0]["labels"]

    def test_retorna_lista_vazia_sem_issues(self) -> None:
        with mock.patch.object(github_transport, "list_issues_by_label", return_value=[]):
            items = github_client.list_by_state("owner/repo", "crewflow:todo")
        assert items == []


class TestSetLabels:
    def test_chama_transport_com_numero_correto(self) -> None:
        with mock.patch.object(github_transport, "set_issue_labels") as m:
            github_client.set_labels("owner/repo", "42", ["crewflow:review"])
            m.assert_called_once_with("owner/repo", 42, ["crewflow:review"])


class TestUpsertStateComment:
    def test_cria_comentario_se_nenhum_existe(self) -> None:
        with mock.patch.object(github_transport, "get_issue_comments", return_value=[]):
            with mock.patch.object(github_transport, "create_issue_comment") as create_m:
                github_client.upsert_state_comment("owner/repo", "1", "<!-- KIRO-FLOW-STATE -->")
                create_m.assert_called_once()

    def test_atualiza_comentario_existente(self) -> None:
        existing = [{"id": 555, "body": f"{norm.STATE_COMMENT_MARKER} old"}]
        with mock.patch.object(github_transport, "get_issue_comments", return_value=existing):
            with mock.patch.object(github_transport, "update_issue_comment") as update_m:
                github_client.upsert_state_comment("owner/repo", "1", "novo body")
                update_m.assert_called_once_with("owner/repo", 555, "novo body")

    def test_nao_cria_duplicata_se_ja_existe(self) -> None:
        existing = [{"id": 555, "body": f"{norm.STATE_COMMENT_MARKER} old"}]
        with mock.patch.object(github_transport, "get_issue_comments", return_value=existing):
            with mock.patch.object(github_transport, "create_issue_comment") as create_m:
                with mock.patch.object(github_transport, "update_issue_comment"):
                    github_client.upsert_state_comment("owner/repo", "1", "body")
                    create_m.assert_not_called()


class TestGetStateComment:
    def test_retorna_corpo_do_comentario_com_marcador(self) -> None:
        body = f"{norm.STATE_COMMENT_MARKER}\nstatus: running"
        with mock.patch.object(github_transport, "get_issue_comments", return_value=[{"body": body}]):
            result = github_client.get_state_comment("owner/repo", "1")
        assert result == body

    def test_retorna_none_se_comentario_ausente(self) -> None:
        with mock.patch.object(github_transport, "get_issue_comments", return_value=[{"body": "normal"}]):
            result = github_client.get_state_comment("owner/repo", "1")
        assert result is None
