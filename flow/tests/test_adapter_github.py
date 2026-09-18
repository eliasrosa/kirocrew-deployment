"""Testes do adapter GitHub.

Todos sem rede — usam ``mock.patch.object`` no transport.
O transport é a fachada patchável: interceptamos lá, nunca no HTTP.
"""

from __future__ import annotations

from unittest import mock

import pytest

from flow.adapters import github_client, github_transport
from flow.adapters import github_normalization as norm
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

        with mock.patch("subprocess.run", return_value=completed), pytest.raises(ProviderSetupError, match="autenticado"):
                github_transport._run(["issue", "view", "1"])

    def test_json_invalido_lanca_provider_error(self) -> None:
        from flow.ports.issue_provider import ProviderError
        completed = mock.MagicMock()
        completed.returncode = 0
        completed.stdout = "não é json"
        completed.stderr = ""

        with mock.patch("subprocess.run", return_value=completed), pytest.raises(ProviderError, match="JSON inválido"):
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
        with mock.patch.object(github_transport, "get_issue", return_value={}), pytest.raises(ProviderNotFoundError):
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
        with mock.patch.object(github_transport, "get_issue_comments", return_value=[]), \
             mock.patch.object(github_transport, "create_issue_comment") as create_m:
            github_client.upsert_state_comment("owner/repo", "1", "<!-- KIRO-FLOW-STATE -->")
            create_m.assert_called_once()

    def test_atualiza_comentario_existente(self) -> None:
        existing = [{"id": 555, "body": f"{norm.STATE_COMMENT_MARKER} old"}]
        with mock.patch.object(github_transport, "get_issue_comments", return_value=existing), \
             mock.patch.object(github_transport, "update_issue_comment") as update_m:
            github_client.upsert_state_comment("owner/repo", "1", "novo body")
            update_m.assert_called_once_with("owner/repo", 555, "novo body")

    def test_nao_cria_duplicata_se_ja_existe(self) -> None:
        existing = [{"id": 555, "body": f"{norm.STATE_COMMENT_MARKER} old"}]
        with mock.patch.object(github_transport, "get_issue_comments", return_value=existing), \
             mock.patch.object(github_transport, "create_issue_comment") as create_m, \
             mock.patch.object(github_transport, "update_issue_comment"):
            github_client.upsert_state_comment("owner/repo", "1", "body")
            create_m.assert_not_called()

    def test_idempotente_apos_multiplas_chamadas(self) -> None:
        """Após 3 chamadas com corpos diferentes, existe 1 comentário com o marker.

        Modela um store stateful de comentários: create acrescenta um comentário
        com o marcador; update substitui in-place o corpo do comentário casado.
        Encoda o critério de aceite da issue #82:
        `jq '[.comments[] | select(.body | contains("KIRO-FLOW-STATE"))] | length' == 1`.
        """
        store: list[dict] = []
        next_id = [100]

        def fake_get_comments(owner_repo: str, number: int) -> list:
            return list(store)

        def fake_create(owner_repo: str, number: int, body: str) -> dict:
            cid = next_id[0]
            next_id[0] += 1
            store.append({"id": cid, "body": body})
            return {"id": cid}

        def fake_update(owner_repo: str, comment_id: int, body: str) -> dict:
            for c in store:
                if c["id"] == comment_id:
                    c["body"] = body
            return {}

        def fake_delete(owner_repo: str, comment_id: int) -> None:
            store[:] = [c for c in store if c["id"] != comment_id]

        bodies = [
            f"{norm.STATE_COMMENT_MARKER}\nstatus: ciclo-{i}\n{norm.STATE_COMMENT_CLOSE}"
            for i in range(3)
        ]

        with mock.patch.object(github_transport, "get_issue_comments", side_effect=fake_get_comments), \
             mock.patch.object(github_transport, "create_issue_comment", side_effect=fake_create), \
             mock.patch.object(github_transport, "update_issue_comment", side_effect=fake_update), \
             mock.patch.object(github_transport, "delete_issue_comment", side_effect=fake_delete):
            for body in bodies:
                github_client.upsert_state_comment("owner/repo", "1", body)

        marker_comments = [c for c in store if norm.STATE_COMMENT_MARKER in c["body"]]
        assert len(marker_comments) == 1
        assert marker_comments[0]["body"] == bodies[-1]

    def test_tres_chamadas_resultam_em_exatamente_um_comentario(self) -> None:
        """Invariante central da issue #82: após N chamadas, a issue tem 1 comentário com o marker.

        Simula o estado da thread após 3 chamadas consecutivas de upsert_state_comment,
        verificando que cada chamada garante o invariante de 1 único comentário.
        """
        # Estado inicial: sem comentários
        db: list[dict] = []
        next_id = [100]

        def fake_get_comments(owner_repo: str, number: int) -> list:
            return list(db)

        def fake_create(owner_repo: str, number: int, body: str) -> dict:
            cid = next_id[0]
            next_id[0] += 1
            db.append({"id": cid, "body": body})
            return {"id": cid}

        def fake_update(owner_repo: str, comment_id: int, body: str) -> dict:
            for c in db:
                if c["id"] == comment_id:
                    c["body"] = body
            return {}

        def fake_delete(owner_repo: str, comment_id: int) -> None:
            db[:] = [c for c in db if c["id"] != comment_id]

        with mock.patch.object(github_transport, "get_issue_comments", side_effect=fake_get_comments), \
             mock.patch.object(github_transport, "create_issue_comment", side_effect=fake_create), \
             mock.patch.object(github_transport, "update_issue_comment", side_effect=fake_update), \
             mock.patch.object(github_transport, "delete_issue_comment", side_effect=fake_delete):

            for i in range(3):
                github_client.upsert_state_comment(
                    "owner/repo", "1",
                    f"{norm.STATE_COMMENT_MARKER}\nstatus: ciclo-{i}\n{norm.STATE_COMMENT_CLOSE}",
                )

        state_comments = [
            c for c in db if norm.STATE_COMMENT_MARKER in c["body"]
        ]
        assert len(state_comments) == 1, (
            f"Esperado 1 comentário com o marker, encontrado {len(state_comments)}"
        )
        assert "ciclo-2" in state_comments[0]["body"]  # conteúdo mais recente

    def test_elimina_duplicatas_acumuladas(self) -> None:
        """Quando já existem N duplicatas acumuladas, upsert restaura o invariante de 1."""
        # Simula estado corrompido: 3 comentários com o marker (gerados por gh issue comment)
        corrupted = [
            {"id": 10, "body": f"{norm.STATE_COMMENT_MARKER}\nstatus: ciclo-0\n{norm.STATE_COMMENT_CLOSE}"},
            {"id": 20, "body": f"{norm.STATE_COMMENT_MARKER}\nstatus: ciclo-1\n{norm.STATE_COMMENT_CLOSE}"},
            {"id": 30, "body": f"{norm.STATE_COMMENT_MARKER}\nstatus: ciclo-2\n{norm.STATE_COMMENT_CLOSE}"},
        ]
        deleted_ids: list[int] = []

        def fake_delete(owner_repo: str, comment_id: int) -> None:
            deleted_ids.append(comment_id)

        with mock.patch.object(github_transport, "get_issue_comments", return_value=corrupted), \
             mock.patch.object(github_transport, "update_issue_comment") as update_m, \
             mock.patch.object(github_transport, "delete_issue_comment", side_effect=fake_delete):
            github_client.upsert_state_comment("owner/repo", "1", "novo body")

        # Deve atualizar o primeiro (id=10)
        update_m.assert_called_once_with("owner/repo", 10, "novo body")
        # Deve deletar os demais (id=20, id=30)
        assert sorted(deleted_ids) == [20, 30]


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

    def test_retorna_primeiro_comentario_consistente_com_upsert(self) -> None:
        """get_state_comment retorna o PRIMEIRO comentário com marker (mesmo que upsert atualiza)."""
        comments = [
            {"id": 10, "body": f"{norm.STATE_COMMENT_MARKER}\nstatus: primeiro"},
            {"id": 20, "body": "sem marker"},
            {"id": 30, "body": f"{norm.STATE_COMMENT_MARKER}\nstatus: segundo"},
        ]
        with mock.patch.object(github_transport, "get_issue_comments", return_value=comments):
            result = github_client.get_state_comment("owner/repo", "1")
        assert result is not None
        assert "primeiro" in result


class TestGetPrForIssue:
    def _make_pr(self, body: str, pr_number: int = 10) -> dict:
        return {
            "number": pr_number,
            "title": "feat: something",
            "headRefName": "feat/issue-42",
            "body": body,
            "mergeable": "MERGEABLE",
        }

    def test_encontra_pr_por_closes(self) -> None:
        pr = self._make_pr("Closes #42\nSome description")
        with mock.patch.object(github_transport, "_run", return_value=[pr]):
            result = github_client.get_pr_for_issue("owner/repo", 42)
        assert result is not None
        assert result["number"] == 10

    def test_encontra_pr_por_fixes(self) -> None:
        pr = self._make_pr("fixes #42")
        with mock.patch.object(github_transport, "_run", return_value=[pr]):
            result = github_client.get_pr_for_issue("owner/repo", 42)
        assert result is not None

    def test_retorna_none_quando_sem_pr(self) -> None:
        pr = self._make_pr("unrelated PR body")
        with mock.patch.object(github_transport, "_run", return_value=[pr]):
            result = github_client.get_pr_for_issue("owner/repo", 42)
        assert result is None

    def test_retorna_none_em_lista_vazia(self) -> None:
        with mock.patch.object(github_transport, "_run", return_value=[]):
            result = github_client.get_pr_for_issue("owner/repo", 42)
        assert result is None

    def test_nao_confunde_numeros_diferentes(self) -> None:
        pr = self._make_pr("Closes #99")
        with mock.patch.object(github_transport, "_run", return_value=[pr]):
            result = github_client.get_pr_for_issue("owner/repo", 42)
        assert result is None


class TestMergePullRequest:
    def test_merge_squash_chama_transport(self) -> None:
        with mock.patch.object(github_transport, "_run", return_value={"merged": True}) as run_m:
            github_client.merge_pull_request("owner/repo", 10)
        run_m.assert_called_once()
        args = run_m.call_args[0][0]
        assert "pulls/10/merge" in " ".join(args)
        assert "squash" in " ".join(args)

    def test_merge_propaga_provider_error(self) -> None:
        from flow.ports.issue_provider import ProviderError
        with mock.patch.object(
            github_transport, "_run",
            side_effect=ProviderError("checks failing"),
        ):
            import pytest
            with pytest.raises(ProviderError, match="checks failing"):
                github_client.merge_pull_request("owner/repo", 10)


class TestGetPrChecks:
    """Testa get_pr_checks via github_transport — zero I/O real."""

    def test_retorna_lista_de_checks(self) -> None:
        checks = [
            {"name": "CI / tests", "state": "success", "conclusion": "success"},
            {"name": "CI / lint", "state": "success", "conclusion": "success"},
        ]
        with mock.patch.object(github_transport, "_run", return_value=checks):
            result = github_client.get_pr_checks("owner/repo", 5)
        assert len(result) == 2
        assert result[0]["name"] == "CI / tests"

    def test_retorna_lista_vazia_quando_sem_checks(self) -> None:
        """Sem checks configurados → lista vazia (não lança erro)."""
        from flow.ports.issue_provider import ProviderError
        with mock.patch.object(
            github_transport, "_run",
            side_effect=ProviderError("no checks"),
        ):
            result = github_client.get_pr_checks("owner/repo", 5)
        assert result == []

    def test_checks_com_failure_retornados(self) -> None:
        checks = [
            {"name": "CI / tests", "state": "failure", "conclusion": "failure"},
        ]
        with mock.patch.object(github_transport, "_run", return_value=checks):
            result = github_client.get_pr_checks("owner/repo", 5)
        assert result[0]["conclusion"] == "failure"

    def test_checks_pendentes_retornados(self) -> None:
        checks = [
            {"name": "CI / tests", "state": "pending", "conclusion": None},
        ]
        with mock.patch.object(github_transport, "_run", return_value=checks):
            result = github_client.get_pr_checks("owner/repo", 5)
        assert result[0]["state"] == "pending"
