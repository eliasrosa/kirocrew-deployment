"""Testes do AzureDevOpsTransport e ScmTransportFactory.

Todos sem rede — usam ``mock.patch`` em ``flow.adapters.scm.azure_devops._request``.
"""

from __future__ import annotations

import os
from unittest import mock
from urllib.error import HTTPError, URLError

import pytest

from flow.adapters.scm import azure_devops as ado
from flow.adapters.scm.factory import ScmRepoConfig, ScmTransportFactory, scm_config_from_repo_entry
from flow.ports.issue_provider import (
    ProviderError,
    ProviderPermissionError,
    ProviderSetupError,
)

# ---------------------------------------------------------------------------
# Fixtures e helpers
# ---------------------------------------------------------------------------

ADO_COORDS = dict(
    org="https://dev.azure.com/kdop",
    project="PlataformaCogna-MKTP-MVP",
    repo="voomp-creators-api-gateway2",
)

ADO_CONFIG = ScmRepoConfig(
    scm="azure_devops",
    azure_org="https://dev.azure.com/kdop",
    azure_project="PlataformaCogna-MKTP-MVP",
    azure_repo="voomp-creators-api-gateway2",
)

GH_CONFIG = ScmRepoConfig(
    scm="github",
    owner_repo="eliasrosa/kirocrew-flow",
)


def _with_pat(fn):
    """Decorador que seta AZURE_DEVOPS_PAT no env durante o teste."""
    def wrapper(*args, **kwargs):
        with mock.patch.dict(os.environ, {"AZURE_DEVOPS_PAT": "test-pat-value"}):
            return fn(*args, **kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------------------
# _pat() — leitura do PAT do ambiente
# ---------------------------------------------------------------------------

class TestPat:
    def test_lanca_setup_error_sem_pat(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "AZURE_DEVOPS_PAT"}
        with (
            mock.patch.dict(os.environ, env, clear=True),
            pytest.raises(ProviderSetupError, match="AZURE_DEVOPS_PAT"),
        ):
            ado._pat()

    def test_retorna_pat_do_ambiente(self) -> None:
        with mock.patch.dict(os.environ, {"AZURE_DEVOPS_PAT": "meu-pat-123"}):
            assert ado._pat() == "meu-pat-123"


# ---------------------------------------------------------------------------
# _auth_header — geração do header Basic
# ---------------------------------------------------------------------------

class TestAuthHeader:
    def test_formato_basic_base64(self) -> None:
        import base64
        header = ado._auth_header("meu-pat")
        encoded = base64.b64encode(b":meu-pat").decode()
        assert header == f"Basic {encoded}"

    def test_header_diferente_para_pats_diferentes(self) -> None:
        h1 = ado._auth_header("pat-a")
        h2 = ado._auth_header("pat-b")
        assert h1 != h2


# ---------------------------------------------------------------------------
# _request — tratamento de erros HTTP
# ---------------------------------------------------------------------------

class TestRequest:
    def _mock_http_error(self, code: int, body: bytes = b"") -> HTTPError:
        class _FakeHTTPError(HTTPError):
            def read(self, amt: object = None) -> bytes:  # type: ignore[override]
                return body

        return _FakeHTTPError(url="http://x", code=code, msg="err", hdrs={}, fp=None)  # type: ignore[arg-type]

    @_with_pat
    def test_401_lanca_setup_error(self) -> None:
        with (
            mock.patch("urllib.request.urlopen", side_effect=self._mock_http_error(401)),
            pytest.raises(ProviderSetupError, match="401"),
        ):
            ado._request("GET", "http://fake/url")

    @_with_pat
    def test_403_lanca_permission_error(self) -> None:
        with (
            mock.patch("urllib.request.urlopen", side_effect=self._mock_http_error(403)),
            pytest.raises(ProviderPermissionError, match="403"),
        ):
            ado._request("GET", "http://fake/url")

    @_with_pat
    def test_404_lanca_provider_error(self) -> None:
        with (
            mock.patch("urllib.request.urlopen", side_effect=self._mock_http_error(404)),
            pytest.raises(ProviderError, match="404"),
        ):
            ado._request("GET", "http://fake/url")

    @_with_pat
    def test_erro_de_rede_lanca_provider_error(self) -> None:
        with (
            mock.patch("urllib.request.urlopen", side_effect=URLError("connection refused")),
            pytest.raises(ProviderError, match="rede"),
        ):
            ado._request("GET", "http://fake/url")


# ---------------------------------------------------------------------------
# create_pull_request
# ---------------------------------------------------------------------------

class TestCreatePullRequest:
    @_with_pat
    def test_chama_request_com_payload_correto(self) -> None:
        response = {"pullRequestId": 99, "title": "feat: algo"}
        with mock.patch.object(ado, "_request", return_value=response) as req_m:
            result = ado.create_pull_request(
                **ADO_COORDS,  # type: ignore[arg-type]
                branch="feat/issue-246",
                title="feat: algo",
                body="Descrição",
            )
        assert result["pullRequestId"] == 99
        req_m.assert_called_once()
        call_body = req_m.call_args[1].get("body") or req_m.call_args[0][2]
        assert call_body["sourceRefName"] == "refs/heads/feat/issue-246"
        assert call_body["targetRefName"] == "refs/heads/main"

    @_with_pat
    def test_inclui_reviewers_quando_fornecidos(self) -> None:
        with mock.patch.object(ado, "_request", return_value={}) as req_m:
            ado.create_pull_request(
                **ADO_COORDS,  # type: ignore[arg-type]
                branch="feat/issue-246",
                title="t",
                body="b",
                reviewers=["uuid-revisor-1"],
            )
        call_body = req_m.call_args[1].get("body") or req_m.call_args[0][2]
        assert call_body.get("reviewers") == [{"id": "uuid-revisor-1"}]

    @_with_pat
    def test_sem_reviewers_nao_inclui_campo(self) -> None:
        with mock.patch.object(ado, "_request", return_value={}) as req_m:
            ado.create_pull_request(
                **ADO_COORDS,  # type: ignore[arg-type]
                branch="feat/issue-246",
                title="t",
                body="b",
            )
        call_body = req_m.call_args[1].get("body") or req_m.call_args[0][2]
        assert "reviewers" not in call_body


# ---------------------------------------------------------------------------
# list_pull_requests
# ---------------------------------------------------------------------------

class TestListPullRequests:
    @_with_pat
    def test_retorna_value_da_resposta(self) -> None:
        prs = [{"pullRequestId": 1}, {"pullRequestId": 2}]
        with mock.patch.object(ado, "_request", return_value={"value": prs}):
            result = ado.list_pull_requests(**ADO_COORDS)
        assert result == prs

    @_with_pat
    def test_retorna_lista_vazia_sem_prs(self) -> None:
        with mock.patch.object(ado, "_request", return_value={"value": []}):
            result = ado.list_pull_requests(**ADO_COORDS)
        assert result == []

    @_with_pat
    def test_filtra_por_branch(self) -> None:
        with mock.patch.object(ado, "_request", return_value={"value": []}) as req_m:
            ado.list_pull_requests(**ADO_COORDS, source_branch="feat/issue-246")
        url: str = req_m.call_args[0][1]
        assert "feat/issue-246" in url


# ---------------------------------------------------------------------------
# get_pull_request
# ---------------------------------------------------------------------------

class TestGetPullRequest:
    @_with_pat
    def test_retorna_dados_do_pr(self) -> None:
        pr_data = {
            "pullRequestId": 5,
            "status": "active",
            "mergeStatus": "succeeded",
            "lastMergeSourceCommit": {"commitId": "abc123"},
        }
        with mock.patch.object(ado, "_request", return_value=pr_data):
            result = ado.get_pull_request(**ADO_COORDS, pr_number=5)
        assert result["pullRequestId"] == 5
        assert result["lastMergeSourceCommit"]["commitId"] == "abc123"


# ---------------------------------------------------------------------------
# merge_pull_request
# ---------------------------------------------------------------------------

class TestMergePullRequest:
    @_with_pat
    def test_merge_busca_pr_e_envia_patch(self) -> None:
        pr_data = {
            "pullRequestId": 5,
            "status": "active",
            "lastMergeSourceCommit": {"commitId": "sha-abc"},
        }
        with mock.patch.object(ado, "_request", side_effect=[pr_data, {"status": "completed"}]) as req_m:
            ado.merge_pull_request(**ADO_COORDS, pr_number=5)
        # Primeira chamada = GET do PR; segunda = PATCH
        assert req_m.call_count == 2
        patch_call = req_m.call_args_list[1]
        assert patch_call[0][0] == "PATCH"
        patch_body = patch_call[1].get("body") or patch_call[0][2]
        assert patch_body["status"] == "completed"
        assert patch_body["completionOptions"]["mergeStrategy"] == "squash"

    @_with_pat
    def test_merge_propaga_provider_error(self) -> None:
        with (
            mock.patch.object(ado, "_request", side_effect=ProviderError("conflict")),
            pytest.raises(ProviderError, match="conflict"),
        ):
            ado.merge_pull_request(**ADO_COORDS, pr_number=5)


# ---------------------------------------------------------------------------
# delete_branch
# ---------------------------------------------------------------------------

class TestDeleteBranch:
    @_with_pat
    def test_delete_branch_existente(self) -> None:
        refs = {"value": [{"name": "refs/heads/feat/123", "objectId": "old-sha"}]}
        with mock.patch.object(ado, "_request", side_effect=[refs, {}]) as req_m:
            ado.delete_branch(**ADO_COORDS, branch="feat/123")
        # Deve ter chamado GET (listar refs) + POST (atualizar para zero-sha)
        assert req_m.call_count == 2

    @_with_pat
    def test_delete_branch_inexistente_silencioso(self) -> None:
        """Branch inexistente (refs vazia) não lança erro."""
        with mock.patch.object(ado, "_request", return_value={"value": []}):
            ado.delete_branch(**ADO_COORDS, branch="feat/inexistente")


# ---------------------------------------------------------------------------
# list_reviews / _normalize_reviewer
# ---------------------------------------------------------------------------

class TestListReviews:
    @_with_pat
    def test_review_aprovado(self) -> None:
        raw = {"value": [{"id": "u1", "displayName": "Elias", "vote": 10, "isRequired": True}]}
        with mock.patch.object(ado, "_request", return_value=raw):
            reviews = ado.list_reviews(**ADO_COORDS, pr_number=5)
        assert len(reviews) == 1
        assert reviews[0]["state"] == "APPROVED"
        assert reviews[0]["user"]["login"] == "Elias"

    @_with_pat
    def test_review_rejeitado(self) -> None:
        raw = {"value": [{"id": "u2", "displayName": "Ana", "vote": -10, "isRequired": False}]}
        with mock.patch.object(ado, "_request", return_value=raw):
            reviews = ado.list_reviews(**ADO_COORDS, pr_number=5)
        assert reviews[0]["state"] == "CHANGES_REQUESTED"

    @_with_pat
    def test_sem_voto_retorna_pending(self) -> None:
        raw = {"value": [{"id": "u3", "displayName": "Bob", "vote": 0, "isRequired": False}]}
        with mock.patch.object(ado, "_request", return_value=raw):
            reviews = ado.list_reviews(**ADO_COORDS, pr_number=5)
        assert reviews[0]["state"] == "PENDING"


class TestNormalizeReviewer:
    def test_vote_10_aprovado(self) -> None:
        r = ado._normalize_reviewer({"id": "x", "displayName": "Bob", "vote": 10, "isRequired": True})
        assert r["state"] == "APPROVED"

    def test_vote_5_aprovado(self) -> None:
        r = ado._normalize_reviewer({"id": "x", "displayName": "Bob", "vote": 5, "isRequired": False})
        assert r["state"] == "APPROVED"

    def test_vote_0_pendente(self) -> None:
        r = ado._normalize_reviewer({"id": "x", "displayName": "Bob", "vote": 0, "isRequired": False})
        assert r["state"] == "PENDING"

    def test_vote_menos_5_changes_requested(self) -> None:
        r = ado._normalize_reviewer({"id": "x", "displayName": "Bob", "vote": -5, "isRequired": False})
        assert r["state"] == "CHANGES_REQUESTED"

    def test_vote_menos_10_changes_requested(self) -> None:
        r = ado._normalize_reviewer({"id": "x", "displayName": "Bob", "vote": -10, "isRequired": False})
        assert r["state"] == "CHANGES_REQUESTED"

    def test_campos_obrigatorios_presentes(self) -> None:
        r = ado._normalize_reviewer({"id": "abc", "displayName": "X", "vote": 0})
        assert "id" in r
        assert "user" in r
        assert "login" in r["user"]
        assert "state" in r
        assert "vote" in r
        assert "isRequired" in r


# ---------------------------------------------------------------------------
# post_comment
# ---------------------------------------------------------------------------

class TestPostComment:
    @_with_pat
    def test_cria_thread_com_conteudo_correto(self) -> None:
        with mock.patch.object(ado, "_request", return_value={"id": 1}) as req_m:
            ado.post_comment(**ADO_COORDS, pr_number=5, body="Revisão aprovada")
        req_m.assert_called_once()
        call_body = req_m.call_args[1].get("body") or req_m.call_args[0][2]
        assert call_body["comments"][0]["content"] == "Revisão aprovada"
        assert call_body["comments"][0]["commentType"] == 1


# ---------------------------------------------------------------------------
# ScmTransportFactory — GitHub
# ---------------------------------------------------------------------------

class TestScmTransportFactoryGitHub:
    def test_factory_github_list_reviews_chama_gh(self) -> None:
        factory = ScmTransportFactory(GH_CONFIG)
        from flow.adapters import github_transport as gh
        with mock.patch.object(gh, "get_pr_reviews", return_value=[]) as m:
            result = factory.list_reviews(5)
        m.assert_called_once_with("eliasrosa/kirocrew-flow", 5)
        assert result == []

    def test_factory_github_delete_branch_chama_gh(self) -> None:
        factory = ScmTransportFactory(GH_CONFIG)
        from flow.adapters import github_transport as gh
        with mock.patch.object(gh, "delete_branch") as m:
            factory.delete_branch("feat/123")
        m.assert_called_once_with("eliasrosa/kirocrew-flow", "feat/123")

    def test_factory_github_get_pr_for_issue(self) -> None:
        factory = ScmTransportFactory(GH_CONFIG)
        from flow.adapters import github_transport as gh
        with mock.patch.object(gh, "get_pr_for_issue", return_value=None):
            result = factory.get_pr_for_issue(42)
        assert result is None

    def test_factory_github_post_comment(self) -> None:
        factory = ScmTransportFactory(GH_CONFIG)
        from flow.adapters import github_transport as gh
        with mock.patch.object(gh, "create_pr_comment", return_value={"id": 1}) as m:
            factory.post_comment(5, "body")
        m.assert_called_once_with("eliasrosa/kirocrew-flow", 5, "body")


# ---------------------------------------------------------------------------
# ScmTransportFactory — Azure DevOps
# ---------------------------------------------------------------------------

class TestScmTransportFactoryAzureDevOps:
    @_with_pat
    def test_factory_ado_list_reviews(self) -> None:
        factory = ScmTransportFactory(ADO_CONFIG)
        reviews = [{"id": "x", "displayName": "Bob", "vote": 10, "isRequired": True}]
        with mock.patch.object(ado, "_request", return_value={"value": reviews}):
            result = factory.list_reviews(10)
        assert result[0]["state"] == "APPROVED"

    @_with_pat
    def test_factory_ado_delete_branch(self) -> None:
        factory = ScmTransportFactory(ADO_CONFIG)
        with mock.patch.object(ado, "_request", return_value={"value": []}) as req_m:
            factory.delete_branch("feat/246")
        # Com refs vazias, deve ter feito GET (list refs) e parado sem chamar POST
        assert req_m.call_count == 1

    @_with_pat
    def test_factory_ado_post_comment(self) -> None:
        factory = ScmTransportFactory(ADO_CONFIG)
        with mock.patch.object(ado, "_request", return_value={"id": 1}) as req_m:
            factory.post_comment(5, "review comment")
        call_body = req_m.call_args[1].get("body") or req_m.call_args[0][2]
        assert call_body["comments"][0]["content"] == "review comment"

    def test_factory_ado_get_pr_for_issue_lanca_not_implemented(self) -> None:
        factory = ScmTransportFactory(ADO_CONFIG)
        with pytest.raises(NotImplementedError, match="Azure DevOps"):
            factory.get_pr_for_issue(42)

    @_with_pat
    def test_factory_ado_merge_mapeia_squash_corretamente(self) -> None:
        factory = ScmTransportFactory(ADO_CONFIG)
        pr_data = {"pullRequestId": 1, "lastMergeSourceCommit": {"commitId": "sha"}}
        with mock.patch.object(ado, "_request", side_effect=[pr_data, {}]) as req_m:
            factory.merge_pull_request(1, merge_method="squash")
        patch_body = req_m.call_args_list[1][1].get("body") or req_m.call_args_list[1][0][2]
        assert patch_body["completionOptions"]["mergeStrategy"] == "squash"

    @_with_pat
    def test_factory_ado_merge_mapeia_merge_para_nofastforward(self) -> None:
        factory = ScmTransportFactory(ADO_CONFIG)
        pr_data = {"pullRequestId": 1, "lastMergeSourceCommit": {"commitId": "sha"}}
        with mock.patch.object(ado, "_request", side_effect=[pr_data, {}]) as req_m:
            factory.merge_pull_request(1, merge_method="merge")
        patch_body = req_m.call_args_list[1][1].get("body") or req_m.call_args_list[1][0][2]
        assert patch_body["completionOptions"]["mergeStrategy"] == "noFastForward"


# ---------------------------------------------------------------------------
# ScmTransportFactory — validação de config
# ---------------------------------------------------------------------------

class TestScmTransportFactoryValidation:
    def test_scm_desconhecido_lanca_value_error(self) -> None:
        bad_config = ScmRepoConfig(scm="unknown_scm")
        with pytest.raises(ValueError, match="não suportado"):
            ScmTransportFactory(bad_config)


# ---------------------------------------------------------------------------
# scm_config_from_repo_entry
# ---------------------------------------------------------------------------

class TestScmConfigFromRepoEntry:
    def test_github_default_quando_scm_omitido(self) -> None:
        entry = {"name": "org/repo"}
        config = scm_config_from_repo_entry(entry)
        assert config.scm == "github"
        assert config.owner_repo == "org/repo"

    def test_github_explicito(self) -> None:
        entry = {"name": "org/repo", "scm": "github"}
        config = scm_config_from_repo_entry(entry)
        assert config.scm == "github"

    def test_azure_devops_completo(self) -> None:
        entry = {
            "name": "kdop/api-gateway2",
            "scm": "azure_devops",
            "azure_org": "https://dev.azure.com/kdop",
            "azure_project": "PlataformaCogna-MKTP-MVP",
            "azure_repo": "voomp-creators-api-gateway2",
        }
        config = scm_config_from_repo_entry(entry)
        assert config.scm == "azure_devops"
        assert config.azure_org == "https://dev.azure.com/kdop"
        assert config.azure_project == "PlataformaCogna-MKTP-MVP"
        assert config.azure_repo == "voomp-creators-api-gateway2"

    def test_factory_construida_com_config_ado(self) -> None:
        entry = {
            "name": "kdop/api-gateway2",
            "scm": "azure_devops",
            "azure_org": "https://dev.azure.com/kdop",
            "azure_project": "Proj",
            "azure_repo": "repo-x",
        }
        config = scm_config_from_repo_entry(entry)
        factory = ScmTransportFactory(config)
        assert factory._config.scm == "azure_devops"

    def test_factory_construida_com_config_github(self) -> None:
        entry = {"name": "org/repo"}
        config = scm_config_from_repo_entry(entry)
        factory = ScmTransportFactory(config)
        assert factory._config.scm == "github"
