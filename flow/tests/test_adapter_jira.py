"""Testes do adapter Jira.

Todos sem rede — usam mock.patch.object no transport.
Cobre as peculiaridades do VGAT que a normalização deve absorver.
"""

from __future__ import annotations

from unittest import mock

import pytest

from flow.adapters import jira_client, jira_transport
from flow.adapters import jira_normalization as norm
from flow.ports.issue_provider import ProviderNotFoundError, ProviderSetupError

# ---------------------------------------------------------------------------
# Helpers de fixture
# ---------------------------------------------------------------------------

def _raw_issue(
    key: str = "VGAT-123",
    summary: str = "[api-gateway2] Fix",
    labels: list[str] | None = None,
    issue_type: str = "Bug",
    parent_key: str | None = None,
) -> dict:
    fields: dict = {
        "summary": summary,
        "labels": labels or [],
        "issuetype": {"name": issue_type},
        "status": {"name": "In Progress"},
        "comment": {"comments": []},
    }
    if parent_key:
        fields["parent"] = {"key": parent_key}
    return {
        "key": key,
        "self": f"https://your-org.atlassian.net/rest/api/2/issue/{key}",
        "fields": fields,
    }


def _search_result(*issues: dict) -> dict:
    return {"issues": list(issues), "total": len(issues)}


# ---------------------------------------------------------------------------
# jira_normalization
# ---------------------------------------------------------------------------

class TestJiraNormalization:
    def test_campos_obrigatorios_presentes(self) -> None:
        item = norm.normalize_item(_raw_issue())
        for campo in ("key", "title", "labels", "state_comment", "parent_key", "url"):
            assert campo in item, f"campo {campo!r} ausente"

    def test_url_convertida_de_api_para_browse(self) -> None:
        item = norm.normalize_item(_raw_issue("VGAT-123"))
        assert "/browse/VGAT-123" in item["url"]
        assert "/rest/api" not in item["url"]

    def test_labels_extraidas(self) -> None:
        item = norm.normalize_item(_raw_issue(labels=["crewflow:todo", "phase-1"]))
        assert "crewflow:todo" in item["labels"]
        assert "phase-1" in item["labels"]

    def test_subtarefa_tem_parent_key(self) -> None:
        raw = _raw_issue(key="VGAT-124", issue_type="Subtarefa", parent_key="VGAT-123")
        item = norm.normalize_item(raw)
        assert item["parent_key"] == "VGAT-123"

    def test_sub_bug_tem_parent_key(self) -> None:
        raw = _raw_issue(key="VGAT-125", issue_type="Sub-bug", parent_key="VGAT-100")
        item = norm.normalize_item(raw)
        assert item["parent_key"] == "VGAT-100"

    def test_bug_normal_nao_tem_parent_key(self) -> None:
        item = norm.normalize_item(_raw_issue(issue_type="Bug"))
        assert item["parent_key"] is None

    def test_enabler_nao_tem_parent_key(self) -> None:
        """Enabler é a task genérica do VGAT — não tem parent."""
        item = norm.normalize_item(_raw_issue(issue_type="Enabler"))
        assert item["parent_key"] is None

    def test_state_comment_extraido_de_comentario_plain(self) -> None:
        raw = _raw_issue()
        raw["fields"]["comment"]["comments"] = [
            {"body": "comentário normal", "id": "1"},
            {"body": f"{norm.STATE_COMMENT_MARKER}\nstatus: running", "id": "2"},
        ]
        item = norm.normalize_item(raw)
        assert item["state_comment"] is not None
        assert "status: running" in item["state_comment"]

    def test_state_comment_none_sem_marcador(self) -> None:
        item = norm.normalize_item(_raw_issue())
        assert item["state_comment"] is None

    def test_labels_hash_deterministico(self) -> None:
        h1 = norm.labels_hash(["crewflow:todo", "phase-1"])
        h2 = norm.labels_hash(["phase-1", "crewflow:todo"])
        assert h1 == h2

    def test_adf_to_plain_extrai_texto(self) -> None:
        from flow.adapters.jira_normalization import _adf_to_plain
        adf = {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "<!-- KIRO-FLOW-STATE -->"},
                    {"type": "text", "text": "status: running"},
                ]},
            ],
        }
        plain = _adf_to_plain(adf)
        assert "KIRO-FLOW-STATE" in plain
        assert "status: running" in plain


# ---------------------------------------------------------------------------
# jira_transport — testes de tratamento de erro
# ---------------------------------------------------------------------------

class TestJiraTransportErrors:
    def test_falta_de_configuracao_lanca_setup_error(self) -> None:
        import os
        env = {k: v for k, v in os.environ.items() if not k.startswith("JIRA_")}
        with mock.patch.dict(os.environ, env, clear=True), \
             pytest.raises(ProviderSetupError, match="JIRA_BASE_URL"):
            jira_transport._get_config()

    def test_http_404_lanca_not_found(self) -> None:
        import urllib.error
        err = urllib.error.HTTPError(url="x", code=404, msg="Not Found", hdrs=None, fp=None)  # type: ignore[arg-type]
        err.read = lambda: b""  # type: ignore[method-assign, misc, assignment]
        with mock.patch("urllib.request.urlopen", side_effect=err), \
             mock.patch.object(jira_transport, "_get_config", return_value=("https://jira.test", {})), \
             pytest.raises(ProviderNotFoundError):
            jira_transport._http_request("GET", "https://jira.test/x", {})

    def test_http_401_lanca_setup_error(self) -> None:
        import urllib.error
        err = urllib.error.HTTPError(url="x", code=401, msg="Unauthorized", hdrs=None, fp=None)  # type: ignore[arg-type]
        err.read = lambda: b""  # type: ignore[method-assign, misc, assignment]
        with mock.patch("urllib.request.urlopen", side_effect=err), \
             mock.patch.object(jira_transport, "_get_config", return_value=("https://jira.test", {})), \
             pytest.raises(ProviderSetupError):
            jira_transport._http_request("GET", "https://jira.test/x", {})


# ---------------------------------------------------------------------------
# jira_client — testes de orquestração (mock no transport)
# ---------------------------------------------------------------------------

class TestGetWorkItem:
    def test_retorna_item_normalizado(self) -> None:
        raw = _raw_issue("VGAT-123", "[api-gateway2] Fix", ["crewflow:dev"])
        with mock.patch.object(jira_transport, "get_issue", return_value=raw):
            item = jira_client.get_work_item("VGAT", "VGAT-123")
        assert item["key"] == "VGAT-123"
        assert "crewflow:dev" in item["labels"]

    def test_issue_vazia_lanca_not_found(self) -> None:
        with mock.patch.object(jira_transport, "get_issue", return_value={}), \
             pytest.raises(ProviderNotFoundError):
            jira_client.get_work_item("VGAT", "VGAT-999")


class TestListByState:
    def test_retorna_lista_normalizada(self) -> None:
        raw = _raw_issue("VGAT-1", labels=["crewflow:todo"])
        with mock.patch.object(jira_transport, "search_issues_by_label",
                               return_value=_search_result(raw)):
            items = jira_client.list_by_state("VGAT", "crewflow:todo")
        assert len(items) == 1
        assert "crewflow:todo" in items[0]["labels"]

    def test_lista_vazia_se_nenhuma_issue(self) -> None:
        with mock.patch.object(jira_transport, "search_issues_by_label",
                               return_value=_search_result()):
            items = jira_client.list_by_state("VGAT", "crewflow:todo")
        assert items == []


class TestSetLabels:
    def test_chama_transport_com_labels(self) -> None:
        with mock.patch.object(jira_transport, "update_issue_labels") as m:
            jira_client.set_labels("VGAT", "VGAT-123", ["crewflow:review"])
            m.assert_called_once_with("VGAT-123", ["crewflow:review"])


class TestUpsertStateComment:
    def test_cria_comentario_se_nenhum_existe(self) -> None:
        with mock.patch.object(jira_transport, "get_issue_comments", return_value=[]), \
             mock.patch.object(jira_transport, "add_issue_comment") as add_m:
            jira_client.upsert_state_comment("VGAT", "VGAT-1", "<!-- KIRO-FLOW-STATE -->")
            add_m.assert_called_once()

    def test_atualiza_comentario_existente(self) -> None:
        existing = [{"id": "10001", "body": f"{norm.STATE_COMMENT_MARKER} old"}]
        with mock.patch.object(jira_transport, "get_issue_comments", return_value=existing), \
             mock.patch.object(jira_transport, "update_comment") as update_m:
            jira_client.upsert_state_comment("VGAT", "VGAT-1", "novo")
            update_m.assert_called_once_with("VGAT-1", "10001", "novo")

    def test_nao_cria_duplicata(self) -> None:
        existing = [{"id": "10001", "body": f"{norm.STATE_COMMENT_MARKER} old"}]
        with mock.patch.object(jira_transport, "get_issue_comments", return_value=existing), \
             mock.patch.object(jira_transport, "add_issue_comment") as add_m, \
             mock.patch.object(jira_transport, "update_comment"):
            jira_client.upsert_state_comment("VGAT", "VGAT-1", "body")
            add_m.assert_not_called()
