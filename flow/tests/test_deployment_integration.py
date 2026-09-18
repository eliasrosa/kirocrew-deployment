"""Testes de integração do deployment.py.

Não testa o dispatch real (precisa do Kiro Crew rodando).
Testa a integração entre deployment → scan → domain.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest import mock

# Adiciona o repo root ao path (como o deployment.py faz em produção)
_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import builtins  # noqa: E402
import tempfile  # noqa: E402

import pytest  # noqa: E402

from deployment.deployment import _load_config, _scan_result_to_issue, run  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _minimal_config(repos: list[str] | None = None, auto: bool = False) -> dict:
    return {
        "repos": repos or ["owner/repo"],
        "auto_dispatch": auto,
        "max_concurrent": 2,
        "one_per_repo": True,
        "notify_chat_id": "",
        "squad_id": "test",
        "issue_provider": "github",
        "dev_root": "/tmp/dev",
        "agent": "kirocrew",
    }


def _make_scan_result(key: str = "https://github.com/owner/repo/issues/42",
                      title: str = "[api-gateway2] Fix",
                      dispatch: bool = True,
                      spec_valid: bool | None = None) -> object:
    from flow.domain.gates import WorkItem
    from flow.domain.state import State
    from flow.scan.scanner import ScanResult

    return ScanResult(
        item=WorkItem(key=key, title=title, labels=frozenset(["crewflow:todo"])),
        current_state=State.TODO,
        modifiers=frozenset(),
        dispatch_candidate=dispatch,
        spec_valid=spec_valid,
        changed=True,
        reason="CANDIDATO A DISPATCH; labels mudaram" if dispatch else "estado: crewflow:dev",
    )


# ---------------------------------------------------------------------------
# _scan_result_to_issue
# ---------------------------------------------------------------------------

class TestScanResultToIssue:
    def test_extrai_numero_de_url_github(self) -> None:
        r = _make_scan_result("https://github.com/owner/repo/issues/42")
        issue = _scan_result_to_issue(r)
        assert issue["number"] == 42

    def test_extrai_numero_de_chave_jira(self) -> None:
        r = _make_scan_result("VGAT-123")
        issue = _scan_result_to_issue(r)
        assert issue["number"] == 123

    def test_title_preservado(self) -> None:
        r = _make_scan_result(title="[api-gw2] Fix urgente")
        issue = _scan_result_to_issue(r)
        assert issue["title"] == "[api-gw2] Fix urgente"


# ---------------------------------------------------------------------------
# _load_config — fallback sem PyYAML usa o parser compartilhado
# ---------------------------------------------------------------------------

class TestLoadConfigFallback:
    """A config do cron deve suportar o mesmo schema de routing que a squad."""

    def test_routing_multilinha_via_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "yaml":
                raise ImportError("PyYAML indisponível (forçado no teste)")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", fake_import)

        cfg_text = (
            "repos:\n"
            "  - owner/repo\n"
            "auto_dispatch: false\n"
            "max_concurrent: 2\n"
            "routing:\n"
            "  - match:\n"
            "      labels:\n"
            "        - crewflow:debt\n"
            "    workflow: debt-flow\n"
            "  - default: feature-flow\n"
        )
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(cfg_text)
            tmp = f.name

        monkeypatch.setattr("deployment.deployment._CONFIG_CANDIDATES", [tmp])
        cfg = _load_config()

        assert cfg["repos"] == ["owner/repo"]
        assert cfg["auto_dispatch"] is False
        assert cfg["max_concurrent"] == 2
        # O routing multi-linha foi parseado com a MESMA estrutura do PyYAML.
        assert cfg["routing"][0] == {
            "match": {"labels": ["crewflow:debt"]},
            "workflow": "debt-flow",
        }
        assert cfg["routing"][-1] == {"default": "feature-flow"}


# ---------------------------------------------------------------------------
# run() — testes de integração com mocks no scan e no ctx
# ---------------------------------------------------------------------------

class TestRunIntegration:
    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def test_notifica_candidatos_em_fase_1(self) -> None:
        """Fase 1 (auto=false): avisa sem despachar."""
        ctx = self._make_ctx()
        results = [_make_scan_result()]

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_minimal_config(auto=False)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=results),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            run(ctx)

        ctx.notify.assert_called_once()
        msg = ctx.notify.call_args[0][0]
        assert "Fase 1" in msg or "ready" in msg.lower()

    def test_notifica_spec_invalida(self) -> None:
        """Spec sem repo flagrada sem dispatch."""
        ctx = self._make_ctx()

        # Precisamos que spec_valid=False seja reconhecido corretamente
        # Ajustamos o mock para retornar o resultado correto
        from flow.domain.gates import WorkItem
        from flow.domain.state import State
        from flow.scan.scanner import ScanResult

        spec_result = ScanResult(
            item=WorkItem(key="VGAT-1", title="Fix sem repo",
                          labels=frozenset(["crewflow:spec"])),
            current_state=State.SPEC,
            modifiers=frozenset(),
            dispatch_candidate=False,
            spec_valid=False,
            changed=True,
            reason="SPEC SEM REPO",
        )

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_minimal_config(auto=False)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[spec_result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            run(ctx)

        ctx.notify.assert_called_once()
        msg = ctx.notify.call_args[0][0]
        assert "spec" in msg.lower() or "repo" in msg.lower()

    def test_sem_resultados_nao_notifica(self) -> None:
        """Sem candidatos: nenhuma notificação."""
        ctx = self._make_ctx()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_minimal_config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            run(ctx)

        ctx.notify.assert_not_called()

    def test_squad_config_inexistente_levanta_erro_early(self) -> None:
        """squad_config apontando para arquivo inexistente deve falhar imediatamente."""
        import pytest

        ctx = self._make_ctx()
        cfg = _minimal_config()
        cfg["squad_config"] = "/tmp/nao-existe-squad-xyz.yaml"

        with (
            mock.patch("deployment.deployment._load_config", return_value=cfg),
            pytest.raises(RuntimeError, match="squad_config"),
        ):
            run(ctx)

        ctx.notify.assert_not_called()


# ---------------------------------------------------------------------------
# Teste de integração do merge automático (GATE 2)
# ---------------------------------------------------------------------------

class TestAutoMergeIntegration:
    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _make_review_scan_result(self) -> object:
        """ScanResult em crewflow:review com crewflow:reviewed."""
        from flow.audit.state_comment import StateComment, render
        from flow.domain.gates import WorkItem
        from flow.domain.state import Modifier, State
        from flow.scan.scanner import ScanResult

        sc = StateComment(
            workflow="feature (v1)", current_node="review",
            status="reviewed", repo="owner/repo",
        )
        sc.set_reviewer_result(approved=True, comments=[], sha="abc123")
        state_body = render(sc)

        return ScanResult(
            item=WorkItem(
                key="https://github.com/owner/repo/issues/42",
                title="[owner/repo] Feature X",
                labels=frozenset(["crewflow:review", "crewflow:reviewed", "crewflow:feature"]),
            ),
            current_state=State.REVIEW,
            modifiers=frozenset([Modifier.REVIEWED]),
            dispatch_candidate=False,
            spec_valid=None,
            changed=True,
            reason="reviewer aprovado",
        ), state_body

    def test_merge_automatico_notifica_tl(self) -> None:
        """Quando reviewer aprova sem comentários, notifica TL com sucesso."""
        from flow.audit.state_comment import StateComment, render
        from flow.domain.gates import WorkItem
        from flow.domain.state import Modifier, State
        from flow.scan.scanner import ScanResult

        ctx = self._make_ctx()

        sc = StateComment(
            workflow="feature (v1)", current_node="review",
            status="reviewed", repo="owner/repo",
        )
        sc.set_reviewer_result(approved=True, comments=[], sha="abc123")
        state_body = render(sc)

        result = ScanResult(
            item=WorkItem(
                key="https://github.com/owner/repo/issues/42",
                title="[owner/repo] Feature X",
                labels=frozenset(["crewflow:review", "crewflow:reviewed", "crewflow:feature"]),
            ),
            current_state=State.REVIEW,
            modifiers=frozenset([Modifier.REVIEWED]),
            dispatch_candidate=False,
            spec_valid=None,
            changed=True,
            reason="reviewer aprovado",
        )

        # Simula get_state_comment retornando o state_body com resultado do reviewer
        def _fake_get_state_comment(repo: str, key: str) -> str:
            return state_body

        fake_pr = {"number": 99, "title": "feat: Feature X", "headRefName": "feat/issue-42", "body": "Closes #42"}

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_minimal_config(auto=True)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("flow.adapters.github_client.get_pr_for_issue",
                       return_value=fake_pr),
            mock.patch("flow.adapters.github_client.merge_pull_request",
                       return_value={"merged": True}),
            mock.patch("flow.adapters.github_client.get_work_item",
                       return_value={"labels": ["crewflow:review", "crewflow:reviewed", "crewflow:feature"]}),
            mock.patch("flow.adapters.github_client.set_labels"),
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider.get_state_comment = _fake_get_state_comment
            mock_provider_for.return_value = mock_provider

            run(ctx)

        # Deve notificar sobre o merge automático
        notify_calls = ctx.notify.call_args_list
        msgs = [str(c) for c in notify_calls]
        assert any("mergead" in m.lower() or "automati" in m.lower() for m in msgs), (
            f"Esperava notificação de merge automático, got: {msgs}"
        )


# ---------------------------------------------------------------------------
# Fix 1 — _repo_has_active / _active_sessions com detecção de locks obsoletos
# ---------------------------------------------------------------------------

class TestRepoHasActiveStaleDetection:
    """_repo_has_active deve ignorar locks mais velhos que 2h."""

    def test_lock_recente_bloqueia(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from deployment.deployment import _repo_has_active

        lock = tmp_path / "dashboard_esteira-myrepo-42.jsonl.lock"
        lock.touch()
        # mtime agora = lock recente
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        assert _repo_has_active("owner/myrepo") is True

    def test_lock_obsoleto_nao_bloqueia(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        import time

        from deployment.deployment import _repo_has_active

        lock = tmp_path / "dashboard_esteira-myrepo-10.jsonl.lock"
        lock.touch()
        # Define mtime como 3h atrás
        old_ts = time.time() - 3 * 3600
        os.utime(str(lock), (old_ts, old_ts))

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        assert _repo_has_active("owner/myrepo") is False

    def test_sem_locks_retorna_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from deployment.deployment import _repo_has_active

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))
        assert _repo_has_active("owner/myrepo") is False

    def test_active_sessions_ignora_locks_obsoletos(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os
        import time

        from deployment.deployment import _active_sessions

        # 1 lock recente + 1 obsoleto
        recente = tmp_path / "dashboard_esteira-repo1-1.jsonl.lock"
        recente.touch()
        obsoleto = tmp_path / "dashboard_esteira-repo2-2.jsonl.lock"
        obsoleto.touch()
        old_ts = time.time() - 3 * 3600
        os.utime(str(obsoleto), (old_ts, old_ts))

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        assert _active_sessions() == 1


# ---------------------------------------------------------------------------
# Fix 2 — _pr_exists / guard contra PR duplicado no dispatch
# ---------------------------------------------------------------------------

class TestPrExists:
    """_pr_exists deve chamar gh pr list e retornar True somente quando há PR aberto."""

    def test_retorna_true_quando_pr_existe(self) -> None:
        from deployment.deployment import _pr_exists

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout='[{"number": 99}]',
                stderr="",
            )
            assert _pr_exists("owner/repo", 42) is True
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "--head" in args
        assert "feat/issue-42" in args

    def test_retorna_false_quando_sem_pr(self) -> None:
        from deployment.deployment import _pr_exists

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="[]", stderr="",
            )
            assert _pr_exists("owner/repo", 42) is False

    def test_retorna_false_em_erro_de_cli(self) -> None:
        from deployment.deployment import _pr_exists

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=1, stdout="", stderr="gh: not authenticated",
            )
            assert _pr_exists("owner/repo", 42) is False

    def test_retorna_false_em_excecao(self) -> None:
        from deployment.deployment import _pr_exists

        with mock.patch("subprocess.run", side_effect=OSError("gh not found")):
            assert _pr_exists("owner/repo", 42) is False


class TestDispatchGuardPrDuplicado:
    """O dispatch deve ser ignorado silenciosamente quando PR já existe."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def test_dispatch_pulado_quando_pr_ja_existe(self) -> None:
        ctx = self._make_ctx()
        results = [_make_scan_result()]

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value={
                           **{
                               "repos": ["owner/repo"],
                               "auto_dispatch": True,
                               "max_concurrent": 2,
                               "one_per_repo": False,  # desativa o guard de lock
                               "notify_chat_id": "",
                               "squad_id": "test",
                               "issue_provider": "github",
                               "dev_root": "/tmp/dev",
                               "agent": "kirocrew",
                           }
                       }),
            mock.patch("deployment.deployment.scan_candidates", return_value=results),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._pr_exists", return_value=True),
            mock.patch("deployment.deployment._dispatch") as mock_disp,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            run(ctx)

        mock_disp.assert_not_called()

    def test_dispatch_executado_quando_sem_pr(self) -> None:
        ctx = self._make_ctx()
        results = [_make_scan_result()]

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value={
                           "repos": ["owner/repo"],
                           "auto_dispatch": True,
                           "max_concurrent": 2,
                           "one_per_repo": False,
                           "notify_chat_id": "",
                           "squad_id": "test",
                           "issue_provider": "github",
                           "dev_root": "/tmp/dev",
                           "agent": "kirocrew",
                       }),
            mock.patch("deployment.deployment.scan_candidates", return_value=results),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._dispatch") as mock_disp,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            run(ctx)

        mock_disp.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #70 — dispatch automático da sessão one-shot do reviewer
# ---------------------------------------------------------------------------

class TestReviewerHasActive:
    """_reviewer_has_active detecta sessão ativa do kiro-reviewer."""

    def test_lock_recente_bloqueia(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from deployment.deployment import _reviewer_has_active

        lock = tmp_path / "dashboard_reviewer-myrepo-42.jsonl.lock"
        lock.touch()
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        assert _reviewer_has_active("owner/myrepo", 42) is True

    def test_lock_obsoleto_nao_bloqueia(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        import time

        from deployment.deployment import _reviewer_has_active

        lock = tmp_path / "dashboard_reviewer-myrepo-42.jsonl.lock"
        lock.touch()
        old_ts = time.time() - 3 * 3600
        os.utime(str(lock), (old_ts, old_ts))
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        assert _reviewer_has_active("owner/myrepo", 42) is False

    def test_sem_lock_retorna_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from deployment.deployment import _reviewer_has_active

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))
        assert _reviewer_has_active("owner/myrepo", 42) is False

    def test_nao_confunde_com_lock_de_outro_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from deployment.deployment import _reviewer_has_active

        # Lock do repo A não afeta repo B
        lock = tmp_path / "dashboard_reviewer-repo-a-42.jsonl.lock"
        lock.touch()
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        assert _reviewer_has_active("owner/repo-b", 42) is False

    def test_nao_confunde_com_lock_de_outro_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from deployment.deployment import _reviewer_has_active

        lock = tmp_path / "dashboard_reviewer-myrepo-100.jsonl.lock"
        lock.touch()
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        assert _reviewer_has_active("owner/myrepo", 99) is False


class TestReviewerPrompt:
    """_reviewer_prompt gera o prompt correto para a sessão one-shot do reviewer."""

    def test_contem_header_com_repo_pr_issue(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "REPO: owner/myrepo" in prompt
        assert "PR: #99" in prompt
        assert "ISSUE: #42" in prompt

    def test_titulo_da_sessao(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "SESSION TITLE: review: myrepo PR #99 (issue #42)" in prompt

    def test_contem_instrucao_de_gh_issue_view(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "gh issue view 42 --repo owner/myrepo" in prompt

    def test_contem_instrucao_de_gh_pr_diff(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "gh pr diff 99 --repo owner/myrepo" in prompt

    def test_contem_instrucao_de_crewflow_reviewed(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "crewflow:reviewed" in prompt

    def test_contem_regra_nunca_merge(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "NUNCA mergeie" in prompt

    def test_short_name_no_titulo(self) -> None:
        """O título usa só o nome curto do repo, não o owner/repo completo."""
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("eliasrosa/kirocrew-flow", pr_number=5, issue_number=70)

        assert "SESSION TITLE: review: kirocrew-flow PR #5 (issue #70)" in prompt


class TestDispatchReviewerFunction:
    """_dispatch_reviewer faz POST /api/chat com o slot e prompt corretos."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _minimal_cfg(self) -> dict:
        return {
            "agent": "kirocrew",
            "notify_chat_id": "",
        }

    def test_busca_pr_pela_branch_correta(self) -> None:
        """_dispatch_reviewer consulta a branch feat/issue-<N> para localizar o PR."""
        from deployment.deployment import _dispatch_reviewer

        ctx = self._make_ctx()
        issue = {"number": 42, "title": "feat: algo"}

        with (
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="[]",  # sem PR — só testamos que consultou a branch certa
                stderr="",
            )
            _dispatch_reviewer(ctx, "owner/repo", issue, self._minimal_cfg())

        # Verifica que consultou gh pr list com --head feat/issue-42
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert "--head" in args
        assert "feat/issue-42" in args
        assert "--repo" in args
        assert "owner/repo" in args

    def test_fallback_notifica_quando_pr_nao_encontrado(self) -> None:
        from deployment.deployment import _dispatch_reviewer

        ctx = self._make_ctx()
        issue = {"number": 42, "title": "feat: algo"}

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0,
                stdout="[]",  # sem PR
                stderr="",
            )
            _dispatch_reviewer(ctx, "owner/repo", issue, self._minimal_cfg())

        # Deve notificar que PR não foi encontrado
        ctx.notify.assert_called_once()
        msg = ctx.notify.call_args[0][0]
        assert "não localizado" in msg or "pendente" in msg or "PR" in msg

    def test_fallback_notifica_quando_cli_falha(self) -> None:
        from deployment.deployment import _dispatch_reviewer

        ctx = self._make_ctx()
        issue = {"number": 42, "title": "feat: algo"}

        with mock.patch("subprocess.run", side_effect=OSError("gh not found")):
            _dispatch_reviewer(ctx, "owner/repo", issue, self._minimal_cfg())

        ctx.notify.assert_called_once()
        msg = ctx.notify.call_args[0][0]
        assert "não localizado" in msg or "pendente" in msg or "PR" in msg


class TestRunDispatchReviewer:
    """Integração: run() despacha sessão one-shot do reviewer quando crewflow:review."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _make_review_scan_result(self) -> object:
        """ScanResult em crewflow:review sem crewflow:reviewed (antes do dispatch)."""
        from flow.domain.gates import WorkItem
        from flow.domain.state import State
        from flow.scan.scanner import ScanResult

        return ScanResult(
            item=WorkItem(
                key="https://github.com/owner/repo/issues/42",
                title="[owner/repo] Feature X",
                labels=frozenset(["crewflow:review", "crewflow:feature"]),
            ),
            current_state=State.REVIEW,
            modifiers=frozenset(),
            dispatch_candidate=False,
            spec_valid=None,
            changed=True,
            reason="review pendente",
        )

    def test_despacha_reviewer_quando_review_sem_reviewed(self) -> None:
        """Quando issue está em crewflow:review, run() chama _dispatch_reviewer."""
        ctx = self._make_ctx()
        result = self._make_review_scan_result()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value={
                           "repos": ["owner/repo"],
                           "auto_dispatch": True,
                           "max_concurrent": 2,
                           "one_per_repo": True,
                           "notify_chat_id": "",
                           "squad_id": "test",
                           "issue_provider": "github",
                           "dev_root": "/tmp/dev",
                           "agent": "kirocrew",
                       }),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._reviewer_has_active", return_value=False),
            mock.patch("deployment.deployment._dispatch_reviewer") as mock_disp_rev,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider.get_state_comment.return_value = None
            mock_provider_for.return_value = mock_provider

            run(ctx)

        mock_disp_rev.assert_called_once()
        call_args = mock_disp_rev.call_args
        assert call_args[0][1] == "owner/repo"   # repo
        assert call_args[0][2]["number"] == 42   # issue["number"]

    def test_nao_despacha_reviewer_quando_ja_ativo(self) -> None:
        """Quando sessão do reviewer já está ativa, run() ignora o dispatch."""
        ctx = self._make_ctx()
        result = self._make_review_scan_result()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value={
                           "repos": ["owner/repo"],
                           "auto_dispatch": True,
                           "max_concurrent": 2,
                           "one_per_repo": True,
                           "notify_chat_id": "",
                           "squad_id": "test",
                           "issue_provider": "github",
                           "dev_root": "/tmp/dev",
                           "agent": "kirocrew",
                       }),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._reviewer_has_active", return_value=True),
            mock.patch("deployment.deployment._dispatch_reviewer") as mock_disp_rev,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider.get_state_comment.return_value = None
            mock_provider_for.return_value = mock_provider

            run(ctx)

        mock_disp_rev.assert_not_called()
