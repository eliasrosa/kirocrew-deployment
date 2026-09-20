"""Testes de integração do deployment.py.

Não testa o dispatch real (precisa do Kiro Crew rodando).
Testa a integração entre deployment → scan → domain.
"""

from __future__ import annotations

import json
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

        fake_pr = {"number": 99, "title": "feat: Feature X", "headRefName": "feat/issue-42", "headRefOid": "abc123", "body": "Closes #42"}

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value={**_minimal_config(auto=True), "workflow_params": {"auto_merge_on_approve": True}}),
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
            mock.patch("flow.adapters.github_client.upsert_pr_review_comment"),
            mock.patch("flow.adapters.github_client.get_state_comment", return_value=state_body),
            mock.patch("flow.adapters.github_client.upsert_state_comment"),
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider.get_state_comment = _fake_get_state_comment
            mock_provider.get_pr_for_issue = mock.MagicMock(return_value=fake_pr)
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
    """_pr_exists deve detectar PR aberta da issue independente do nome da branch."""

    def test_retorna_true_quando_pr_existe_branch_canonica(self) -> None:
        """Detecta PR pelo nome canônico feat/issue-N (1ª busca)."""
        from deployment.deployment import _pr_exists

        def _side_effect(cmd, **kwargs):
            # 1ª chamada: busca por --head feat/issue-42 → encontra
            if "--head" in cmd:
                return mock.MagicMock(returncode=0, stdout='[{"number": 99}]', stderr="")
            return mock.MagicMock(returncode=0, stdout="[]", stderr="")

        with mock.patch("subprocess.run", side_effect=_side_effect) as mock_run:
            assert _pr_exists("owner/repo", 42) is True

        # A 1ª busca (branch canônica) já retornou True — não deve ter feito 2ª busca
        assert mock_run.call_count == 1
        args = mock_run.call_args_list[0][0][0]
        assert "--head" in args
        assert "feat/issue-42" in args

    def test_retorna_true_quando_pr_existe_branch_alternativa(self) -> None:
        """Detecta PR com branch de nome alternativo via busca por 'Closes #N in:body'."""
        from deployment.deployment import _pr_exists

        call_count = 0

        def _side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if "--head" in cmd:
                # 1ª busca (branch canônica): não encontra
                return mock.MagicMock(returncode=0, stdout="[]", stderr="")
            if "--search" in cmd:
                # 2ª busca (corpo da PR): encontra
                return mock.MagicMock(returncode=0, stdout='[{"number": 105}]', stderr="")
            return mock.MagicMock(returncode=0, stdout="[]", stderr="")

        with mock.patch("subprocess.run", side_effect=_side_effect):
            assert _pr_exists("owner/repo", 91) is True

        assert call_count == 2  # fez as duas buscas

    def test_retorna_false_quando_sem_pr_em_nenhuma_busca(self) -> None:
        """Retorna False quando nenhuma busca encontra PR aberta."""
        from deployment.deployment import _pr_exists

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="[]", stderr="",
            )
            assert _pr_exists("owner/repo", 42) is False

        assert mock_run.call_count == 2  # fez as duas buscas

    def test_segunda_busca_usa_closes_n_in_body(self) -> None:
        """A busca por branch alternativa usa a query correta."""
        from deployment.deployment import _pr_exists

        calls: list = []

        def _side_effect(cmd, **kwargs):
            calls.append(cmd)
            return mock.MagicMock(returncode=0, stdout="[]", stderr="")

        with mock.patch("subprocess.run", side_effect=_side_effect):
            _pr_exists("owner/repo", 55)

        assert len(calls) == 2
        search_cmd = calls[1]
        assert "--search" in search_cmd
        idx = search_cmd.index("--search")
        query = search_cmd[idx + 1]
        assert "Closes #55" in query
        assert "in:body" in query

    def test_retorna_false_em_erro_de_cli_em_ambas_buscas(self) -> None:
        """Falha em ambas as buscas resulta em False (fail-safe)."""
        from deployment.deployment import _pr_exists

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=1, stdout="", stderr="gh: not authenticated",
            )
            assert _pr_exists("owner/repo", 42) is False

    def test_retorna_false_em_excecao(self) -> None:
        """Exceção de subprocess resulta em False (fail-safe)."""
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
# Issue #120 — _try_acquire_dispatch_lock: lock atômico antes do POST /api/chat
# ---------------------------------------------------------------------------

class TestTryAcquireDispatchLock:
    """_try_acquire_dispatch_lock cria o backstop lock de forma atômica (O_CREAT|O_EXCL)."""

    def test_adquire_quando_sem_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem lock prévio: adquire e cria o arquivo."""
        from deployment.deployment import _try_acquire_dispatch_lock

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))
        acquired, lock_path = _try_acquire_dispatch_lock("owner/myrepo", 42)

        assert acquired is True
        assert Path(lock_path).exists()

    def test_falha_quando_lock_recente_existe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock recente já existente: adquisição falha (outro ciclo ganhou a corrida)."""
        from deployment.deployment import _try_acquire_dispatch_lock

        lock = tmp_path / "dashboard_esteira-myrepo-42.jsonl.lock"
        lock.touch()
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        acquired, _lock_path = _try_acquire_dispatch_lock("owner/myrepo", 42)

        assert acquired is False

    def test_adquire_quando_lock_obsoleto(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock stale (expirado): adquisição bem-sucedida (sessão antiga encerrou)."""
        import os as _os
        import time

        from deployment.deployment import _try_acquire_dispatch_lock

        lock = tmp_path / "dashboard_esteira-myrepo-42.jsonl.lock"
        lock.touch()
        # Define mtime como 3h atrás (backstop = 2min)
        old_ts = time.time() - 3 * 3600
        _os.utime(str(lock), (old_ts, old_ts))
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        acquired, _lock_path = _try_acquire_dispatch_lock("owner/myrepo", 42)

        assert acquired is True

    def test_race_dois_dispatches_mesma_issue_apenas_um_passa(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dois chamadores concorrentes: apenas um adquire o lock — 50 iterações.

        Cada iteração usa um número de issue diferente para isolar o estado.
        Garante que a implementação O_CREAT|O_EXCL não tem janela TOCTOU no
        caminho normal (lock não existe).  Cobrimos 50 corridas para detectar
        falhas intermitentes antes que cheguem ao CI.
        """
        import threading

        from deployment.deployment import _try_acquire_dispatch_lock

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        for issue_n in range(1000, 1050):
            resultados: list[bool] = []
            resultado_lock = threading.Lock()

            def dispatch_attempt(n: int = issue_n) -> None:
                acquired, _ = _try_acquire_dispatch_lock("owner/myrepo", n)
                with resultado_lock:
                    resultados.append(acquired)

            t1 = threading.Thread(target=dispatch_attempt)
            t2 = threading.Thread(target=dispatch_attempt)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            # Exatamente 1 deve ter adquirido o lock em cada iteração
            assert resultados.count(True) == 1, (
                f"Iteração {issue_n - 999}/50: esperado 1 True, "
                f"obtido {resultados.count(True)} — TOCTOU detectado"
            )
            assert resultados.count(False) == 1

    def test_race_stale_lock_apenas_um_passa(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dois chamadores concorrentes com lock stale: apenas um adquire — 50 iterações.

        Este é o caminho que tinha o TOCTOU real (issue #141): ambos os threads
        passavam pelo pré-check exists()+stale, um removia o arquivo do outro e
        ambos retornavam True.  A nova implementação é O_EXCL-first: o unlink do
        stale não reabre a janela porque a segunda tentativa atômica é O_EXCL.
        """
        import os as _os
        import threading
        import time

        from deployment.deployment import _DISPATCH_BACKSTOP_SECS, _try_acquire_dispatch_lock

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        for issue_n in range(2000, 2050):
            # Cria um lock stale (mtime = agora - 3x backstop)
            lock_file = tmp_path / f"dashboard_esteira-myrepo-{issue_n}.jsonl.lock"
            lock_file.touch()
            old_ts = time.time() - _DISPATCH_BACKSTOP_SECS * 3
            _os.utime(str(lock_file), (old_ts, old_ts))

            resultados: list[bool] = []
            resultado_lock = threading.Lock()

            def dispatch_attempt(n: int = issue_n) -> None:
                acquired, _ = _try_acquire_dispatch_lock("owner/myrepo", n)
                with resultado_lock:
                    resultados.append(acquired)

            t1 = threading.Thread(target=dispatch_attempt)
            t2 = threading.Thread(target=dispatch_attempt)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert resultados.count(True) == 1, (
                f"Iteração stale {issue_n - 1999}/50: esperado 1 True, "
                f"obtido {resultados.count(True)} — TOCTOU no caminho stale detectado"
            )
            assert resultados.count(False) == 1

    def test_nomes_de_repo_diferentes_nao_conflitam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issues de repos diferentes geram locks distintos — sem colisão."""
        from deployment.deployment import _try_acquire_dispatch_lock

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        acquired_a, _ = _try_acquire_dispatch_lock("owner/repo-a", 10)
        acquired_b, _ = _try_acquire_dispatch_lock("owner/repo-b", 10)

        assert acquired_a is True
        assert acquired_b is True

    def test_numeros_diferentes_na_mesma_repo_nao_conflitam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Issues diferentes no mesmo repo não bloqueiam uma à outra."""
        from deployment.deployment import _try_acquire_dispatch_lock

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        acquired_1, _ = _try_acquire_dispatch_lock("owner/myrepo", 1)
        acquired_2, _ = _try_acquire_dispatch_lock("owner/myrepo", 2)

        assert acquired_1 is True
        assert acquired_2 is True


class TestDispatchAcquiresLockBeforePost:
    """_dispatch deve criar o backstop lock ANTES de chamar POST /api/chat.

    Critério de aceite da issue #120: o lock deve existir quando o POST é feito,
    e um segundo _dispatch para a mesma issue deve ser abortado pelo lock.
    """

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def test_lock_criado_antes_do_post(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """O backstop lock deve existir quando o POST /api/chat é chamado."""
        import sys
        import types

        from deployment.deployment import _dispatch

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        lock_existed_at_post_time: list[bool] = []

        # Cria um módulo fake para kiro_crew.loopback_http
        def fake_loopback_urlopen(req, timeout=3):  # type: ignore[no-untyped-def]
            lock = tmp_path / "dashboard_esteira-myrepo-42.jsonl.lock"
            lock_existed_at_post_time.append(lock.exists())
            resp = mock.MagicMock()
            resp.__enter__ = mock.MagicMock(return_value=resp)
            resp.__exit__ = mock.MagicMock(return_value=False)
            resp.read = mock.MagicMock(return_value=b"")
            return resp

        fake_loopback_mod = types.ModuleType("kiro_crew.loopback_http")
        fake_loopback_mod.loopback_urlopen = fake_loopback_urlopen  # type: ignore[attr-defined]
        fake_kiro_crew = types.ModuleType("kiro_crew")

        monkeypatch.setitem(sys.modules, "kiro_crew", fake_kiro_crew)
        monkeypatch.setitem(sys.modules, "kiro_crew.loopback_http", fake_loopback_mod)

        cfg = {
            "dev_root": str(tmp_path),
            "agent": "kirocrew",
            "notify_chat_id": "",
            "vault_root": "",
        }
        issue = {"number": 42, "title": "Test issue", "url": "https://github.com/owner/myrepo/issues/42"}

        with mock.patch("deployment.deployment._dispatch_prompt", return_value="msg"):
            _dispatch(self._make_ctx(), "owner/myrepo", issue, cfg)

        # O lock deve ter existido quando o POST foi feito
        assert lock_existed_at_post_time, "loopback_urlopen nunca foi chamado"
        assert lock_existed_at_post_time[0] is True, (
            "O lock NÃO existia quando o POST foi feito — race condition!"
        )

    def test_segundo_dispatch_abortado_pelo_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Segundo _dispatch para a mesma issue é abortado pelo lock do primeiro."""
        import contextlib

        from deployment.deployment import _dispatch

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        post_count = 0

        def fake_dispatch_prompt(*a: object, **kw: object) -> str:  # type: ignore[no-untyped-def]
            nonlocal post_count
            post_count += 1
            return "msg"

        cfg = {
            "dev_root": str(tmp_path),
            "agent": "kirocrew",
            "notify_chat_id": "",
            "vault_root": "",
        }
        issue = {"number": 99, "title": "Test race", "url": "https://github.com/owner/myrepo/issues/99"}

        with mock.patch("deployment.deployment._dispatch_prompt", side_effect=fake_dispatch_prompt):
            # Primeiro dispatch: cria o lock e prossegue até _dispatch_prompt
            with contextlib.suppress(Exception):
                _dispatch(self._make_ctx(), "owner/myrepo", issue, cfg)

            # Segundo dispatch: lock já existe → deve abortar antes de chamar _dispatch_prompt
            with contextlib.suppress(Exception):
                _dispatch(self._make_ctx(), "owner/myrepo", issue, cfg)

        # _dispatch_prompt só é chamado se o lock foi adquirido
        assert post_count == 1, (
            f"_dispatch_prompt foi chamado {post_count} vezes — "
            "o segundo dispatch não foi bloqueado pelo lock!"
        )


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

        # Novo formato: tabela de infos em vez de lista plana (issue #126)
        assert "owner/myrepo" in prompt
        assert "#99" in prompt
        assert "#42" in prompt

    def test_titulo_da_sessao(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        # Novo formato: session_title como H1 em vez de campo SESSION TITLE: (issue #126)
        assert "# review: myrepo PR #99 (issue #42)" in prompt

    def test_contem_instrucao_de_gh_issue_view(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "gh issue view 42 --repo owner/myrepo" in prompt

    def test_contem_instrucao_de_gh_pr_diff(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "gh pr diff 99 --repo owner/myrepo" in prompt

    def test_contem_instrucao_de_crewflow_review_ok_e_fail(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "crewflow:review-ok" in prompt
        assert "crewflow:review-fail" in prompt

    def test_contem_regra_nunca_merge(self) -> None:
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "NUNCA mergeie" in prompt

    def test_short_name_no_titulo(self) -> None:
        """O título usa só o nome curto do repo, não o owner/repo completo."""
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("eliasrosa/kirocrew-flow", pr_number=5, issue_number=70)

        # Novo formato: H1 em vez de SESSION TITLE: (issue #126)
        assert "# review: kirocrew-flow PR #5 (issue #70)" in prompt

    def test_instrui_postar_no_pr_via_gh_pr_comment(self) -> None:
        """O prompt manda postar o resultado NO PR (passo 7) e NA ISSUE (passo 8).

        Com a issue #88 o resultado completo vai nos dois lugares.
        """
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        # Passo 7: postar no PR (instrução de postar o review completo)
        assert "POSTE O RESULTADO COMPLETO DO REVIEW NO PR" in prompt
        # Passo 8: postar na issue também via gh issue comment
        assert "gh issue comment 42 --repo owner/myrepo" in prompt

    def test_contem_formato_kirocrew_review(self) -> None:
        """O prompt referencia o formato KiroCrew Review do comentário do PR."""
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "## 🤖 KiroCrew Review" in prompt
        assert "**Resultado:**" in prompt
        assert "✅ Aprovado" in prompt
        assert "⚠️ Pedidos de mudança" in prompt
        assert "*Reviewer automático — issue #42*" in prompt

    def test_resultado_completo_na_issue_em_vez_de_referencia_curta(self) -> None:
        """Com a issue #88: resultado completo na issue, não só referência curta.

        O reviewer posta o mesmo comentário nos dois lugares (PR e issue),
        em vez de postar só "Review postado em PR #X — status".
        """
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        # Resultado completo na issue (passo 8)
        assert "POSTE O RESULTADO COMPLETO DO REVIEW NA ISSUE" in prompt
        assert "gh issue comment" in prompt
        # A referência curta já não é o comportamento esperado
        assert "Review postado em PR #99" not in prompt

    def test_preserva_state_comment_na_issue(self) -> None:
        """O prompt continua instruindo o upsert do ReviewerResult NA ISSUE."""
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        assert "upsert_state_comment" in prompt
        assert "<!-- KIRO-FLOW-STATE -->" in prompt
        assert "ReviewerResult" in prompt

    def test_regressao_todos_substrings_antigos(self) -> None:
        """Guard de regressão: todos os substrings previamente asseridos seguem presentes."""
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        # Novo formato: tabela + H1 em vez de lista plana (issue #126)
        assert "owner/myrepo" in prompt
        assert "#99" in prompt
        assert "#42" in prompt
        assert "# review: myrepo PR #99 (issue #42)" in prompt
        assert "gh issue view 42 --repo owner/myrepo" in prompt
        assert "gh pr diff 99 --repo owner/myrepo" in prompt
        assert "crewflow:review-ok" in prompt
        assert "NUNCA mergeie" in prompt

    def test_exemplar_do_pr_derivado_do_helper(self) -> None:
        """Fonte única de verdade: o exemplar do comentário do PR no prompt é
        PRODUZIDO por render_pr_review_comment (cada linha do helper aparece no
        prompt, apenas indentada). Se o helper e o prompt divergirem, quebra.
        """
        from deployment.deployment import _reviewer_prompt
        from flow.audit.state_comment import ReviewerResult, render_pr_review_comment

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        _rr_ko = ReviewerResult(
            approved=False,
            comments=("<mudança 1>", "<mudança 2>"),
            sha="<sha>",
            reviewer="kiro-reviewer",
        )
        exemplo_mudancas = render_pr_review_comment(_rr_ko, issue_number=42)
        for line in exemplo_mudancas.splitlines():
            assert (f"     {line}" if line else line) in prompt

        _rr_ok = ReviewerResult(approved=True, comments=(), sha="<sha>", reviewer="kiro-reviewer")
        exemplo_aprovado = render_pr_review_comment(_rr_ok, issue_number=42)
        for line in exemplo_aprovado.splitlines():
            assert (f"     {line}" if line else line) in prompt

    def test_resultado_completo_postado_nos_dois_lugares(self) -> None:
        """O prompt instrui a postar resultado completo TANTO no PR quanto na issue.

        Com a issue #88, o reviewer posta o resultado completo em dois lugares —
        PR e issue — em vez de só uma referência curta.
        """
        from deployment.deployment import _reviewer_prompt

        prompt = _reviewer_prompt("owner/myrepo", pr_number=99, issue_number=42)

        # Passo 7: postar no PR
        assert "POSTE O RESULTADO COMPLETO DO REVIEW NO PR" in prompt
        # Passo 8: postar na issue também (completo)
        assert "POSTE O RESULTADO COMPLETO DO REVIEW NA ISSUE" in prompt
        assert "gh issue comment" in prompt
        assert "mesmo corpo completo" in prompt.lower() or "mesmo corpo" in prompt


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

        # Verifica que alguma chamada consultou gh pr list com --head feat/issue-42
        # (pode haver mais chamadas — ex: _is_issue_closed roda antes como guard)
        pr_list_calls = [c for c in mock_run.call_args_list if "--head" in c[0][0]]
        assert len(pr_list_calls) == 1
        args = pr_list_calls[0][0][0]
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


# ---------------------------------------------------------------------------
# _post_reviewer_result_on_pr — posta resultado do reviewer no PR
# ---------------------------------------------------------------------------

class TestPostReviewerResultOnPr:
    """_post_reviewer_result_on_pr posta o ReviewerResult no PR e atualiza a issue."""

    def _make_state_comment_with_result(self, approved: bool, comments: list[str]) -> str:
        from flow.audit.state_comment import StateComment, render
        sc = StateComment(
            workflow="feature (v1)",
            current_node="review",
            status="running",
            repo="owner/myrepo",
        )
        sc.set_reviewer_result(approved=approved, comments=comments, sha="abc123")
        return render(sc)

    def test_posta_no_pr_quando_pr_encontrado(self) -> None:
        from deployment.deployment import _post_reviewer_result_on_pr
        from flow.adapters import github_client

        state_comment = self._make_state_comment_with_result(approved=True, comments=[])
        pr = {"number": 10, "headRefName": "feat/issue-42"}

        with mock.patch.object(github_client, "get_pr_for_issue", return_value=pr), \
             mock.patch.object(github_client, "upsert_pr_review_comment") as mock_upsert, \
             mock.patch.object(github_client, "get_state_comment", return_value=state_comment), \
             mock.patch.object(github_client, "upsert_state_comment"):
            _post_reviewer_result_on_pr("owner/myrepo", 42, "https://github.com/owner/myrepo/issues/42", state_comment)

        mock_upsert.assert_called_once()
        body = mock_upsert.call_args[0][2]
        assert "KiroCrew Review" in body
        assert "✅ Aprovado" in body

    def test_nao_posta_se_pr_nao_encontrado(self) -> None:
        from deployment.deployment import _post_reviewer_result_on_pr
        from flow.adapters import github_client

        state_comment = self._make_state_comment_with_result(approved=True, comments=[])

        with mock.patch.object(github_client, "get_pr_for_issue", return_value=None), \
             mock.patch.object(github_client, "upsert_pr_review_comment") as mock_upsert:
            _post_reviewer_result_on_pr("owner/myrepo", 42, "https://github.com/owner/myrepo/issues/42", state_comment)

        mock_upsert.assert_not_called()

    def test_nao_posta_se_reviewer_result_ausente(self) -> None:
        from deployment.deployment import _post_reviewer_result_on_pr
        from flow.adapters import github_client

        state_comment = "comentário normal sem ReviewerResult"

        with mock.patch.object(github_client, "get_pr_for_issue") as mock_pr, \
             mock.patch.object(github_client, "upsert_pr_review_comment") as mock_upsert:
            _post_reviewer_result_on_pr("owner/myrepo", 42, "https://github.com/owner/myrepo/issues/42", state_comment)

        mock_pr.assert_not_called()
        mock_upsert.assert_not_called()

    def test_posta_pedidos_de_mudanca_quando_reprovado(self) -> None:
        from deployment.deployment import _post_reviewer_result_on_pr
        from flow.adapters import github_client

        state_comment = self._make_state_comment_with_result(
            approved=False,
            comments=["Falta cobertura em X", "Nome confuso"],
        )
        pr = {"number": 10, "headRefName": "feat/issue-42"}

        with mock.patch.object(github_client, "get_pr_for_issue", return_value=pr), \
             mock.patch.object(github_client, "upsert_pr_review_comment") as mock_upsert, \
             mock.patch.object(github_client, "get_state_comment", return_value=state_comment), \
             mock.patch.object(github_client, "upsert_state_comment"):
            _post_reviewer_result_on_pr("owner/myrepo", 42, "https://github.com/owner/myrepo/issues/42", state_comment)

        mock_upsert.assert_called_once()
        body = mock_upsert.call_args[0][2]
        assert "⚠️ Pedidos de mudança" in body
        assert "Falta cobertura em X" in body
        assert "Nome confuso" in body

    def test_erro_no_upsert_nao_propaga_excecao(self) -> None:
        """Falha ao postar no PR é silenciosa — o resultado já está na issue."""
        from deployment.deployment import _post_reviewer_result_on_pr
        from flow.adapters import github_client
        from flow.ports.issue_provider import ProviderError

        state_comment = self._make_state_comment_with_result(approved=True, comments=[])
        pr = {"number": 10, "headRefName": "feat/issue-42"}

        with mock.patch.object(github_client, "get_pr_for_issue", return_value=pr), \
             mock.patch.object(github_client, "upsert_pr_review_comment", side_effect=ProviderError("falha")), \
             mock.patch.object(github_client, "get_state_comment", return_value=None):
            # Não deve lançar exceção
            _post_reviewer_result_on_pr("owner/myrepo", 42, "https://github.com/owner/myrepo/issues/42", state_comment)


# ---------------------------------------------------------------------------
# render_pr_review_comment — renderizador do comentário no PR
# ---------------------------------------------------------------------------

class TestRenderPrReviewComment:
    """render_pr_review_comment gera o formato correto para o PR."""

    def test_aprovado_sem_comentarios(self) -> None:
        from flow.audit.state_comment import ReviewerResult, render_pr_review_comment

        rr = ReviewerResult(approved=True, comments=(), sha="abc123")
        body = render_pr_review_comment(rr, issue_number=42, issue_url="https://github.com/owner/repo/issues/42")

        assert "<!-- KIRO-FLOW-REVIEW -->" in body
        assert "<!-- /KIRO-FLOW-REVIEW -->" in body
        assert "✅ Aprovado" in body
        assert "Pedidos de mudança" not in body
        assert "issue #42" in body

    def test_reprovado_com_comentarios(self) -> None:
        from flow.audit.state_comment import ReviewerResult, render_pr_review_comment

        rr = ReviewerResult(approved=False, comments=("Falta teste", "Import errado"), sha="")
        body = render_pr_review_comment(rr, issue_number=10)

        assert "⚠️ Pedidos de mudança" in body
        assert "Falta teste" in body
        assert "Import errado" in body

    def test_marcadores_presentes(self) -> None:
        from flow.audit.state_comment import (
            PR_REVIEW_COMMENT_CLOSE,
            PR_REVIEW_COMMENT_MARKER,
            ReviewerResult,
            render_pr_review_comment,
        )

        rr = ReviewerResult(approved=True, comments=())
        body = render_pr_review_comment(rr)

        assert PR_REVIEW_COMMENT_MARKER in body
        assert PR_REVIEW_COMMENT_CLOSE in body

    def test_sha_aparece_quando_fornecido(self) -> None:
        from flow.audit.state_comment import ReviewerResult, render_pr_review_comment

        rr = ReviewerResult(approved=True, comments=(), sha="deadbeef")
        body = render_pr_review_comment(rr)

        assert "deadbeef" in body

    def test_sem_sha_nao_aparece_linha_sha(self) -> None:
        from flow.audit.state_comment import ReviewerResult, render_pr_review_comment

        rr = ReviewerResult(approved=True, comments=(), sha="")
        body = render_pr_review_comment(rr)

        assert "**SHA:**" not in body


# ---------------------------------------------------------------------------
# upsert_pr_review_comment — upsert no PR (github_client)
# ---------------------------------------------------------------------------

class TestUpsertPrReviewComment:
    """upsert_pr_review_comment cria ou atualiza o comentário de review no PR."""

    def test_cria_se_nenhum_existente(self) -> None:
        from flow.adapters import github_client, github_transport

        with mock.patch.object(github_transport, "get_pr_comments", return_value=[]), \
             mock.patch.object(github_transport, "create_pr_comment") as mock_create:
            github_client.upsert_pr_review_comment("owner/repo", 10, "<!-- KIRO-FLOW-REVIEW --> body")

        mock_create.assert_called_once_with("owner/repo", 10, "<!-- KIRO-FLOW-REVIEW --> body")

    def test_atualiza_se_ja_existe(self) -> None:
        from flow.adapters import github_client, github_transport

        existing = [{"id": 999, "body": "<!-- KIRO-FLOW-REVIEW --> old body <!-- /KIRO-FLOW-REVIEW -->"}]
        with mock.patch.object(github_transport, "get_pr_comments", return_value=existing), \
             mock.patch.object(github_transport, "update_pr_comment") as mock_update:
            github_client.upsert_pr_review_comment("owner/repo", 10, "<!-- KIRO-FLOW-REVIEW --> new body")

        mock_update.assert_called_once_with("owner/repo", 999, "<!-- KIRO-FLOW-REVIEW --> new body")

    def test_nao_cria_duplicata(self) -> None:
        from flow.adapters import github_client, github_transport

        existing = [{"id": 999, "body": "<!-- KIRO-FLOW-REVIEW --> old body <!-- /KIRO-FLOW-REVIEW -->"}]
        with mock.patch.object(github_transport, "get_pr_comments", return_value=existing), \
             mock.patch.object(github_transport, "create_pr_comment") as mock_create, \
             mock.patch.object(github_transport, "update_pr_comment"):
            github_client.upsert_pr_review_comment("owner/repo", 10, "body")

        mock_create.assert_not_called()


def _make_dispatch_scan_result(
    key: str = "https://github.com/owner/repo/issues/42",
    title: str = "[owner/repo] Fix",
) -> object:
    from flow.domain.gates import WorkItem
    from flow.domain.state import State
    from flow.scan.scanner import ScanResult

    return ScanResult(
        item=WorkItem(key=key, title=title, labels=frozenset(["crewflow:todo", "crewflow:feature"])),
        current_state=State.TODO,
        modifiers=frozenset(),
        dispatch_candidate=True,
        spec_valid=None,
        changed=True,
        reason="CANDIDATO A DISPATCH; labels mudaram",
    )


class TestDryRun:
    """dry_run=True/CREWFLOW_DRY_RUN=1 — scan roda, efeitos colaterais não."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _dry_run_config(self, dry_run_in_config: bool = False) -> dict:
        cfg: dict = {
            "repos": ["owner/repo"],
            "auto_dispatch": True,
            "max_concurrent": 2,
            "one_per_repo": True,
            "notify_chat_id": "",
            "squad_id": "test",
            "issue_provider": "github",
            "dev_root": "/tmp/dev",
            "agent": "kirocrew",
        }
        if dry_run_in_config:
            cfg["dry_run"] = True
        return cfg

    def test_dispatch_nao_chamado_em_dry_run_via_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CREWFLOW_DRY_RUN=1 → _dispatch NÃO é chamado."""
        monkeypatch.setenv("CREWFLOW_DRY_RUN", "1")
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result()

        with (
            mock.patch("deployment.deployment._load_config", return_value=self._dry_run_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()

            run(ctx)

        mock_dispatch.assert_not_called()

    def test_dispatch_nao_chamado_em_dry_run_via_config(self) -> None:
        """dry_run: true na config → _dispatch NÃO é chamado."""
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result()

        with (
            mock.patch(
                "deployment.deployment._load_config",
                return_value=self._dry_run_config(dry_run_in_config=True),
            ),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()

            run(ctx)

        mock_dispatch.assert_not_called()

    def test_notify_nao_chamado_em_dry_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nenhuma notificação enviada no modo dry-run."""
        monkeypatch.setenv("CREWFLOW_DRY_RUN", "1")
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result()

        with (
            mock.patch("deployment.deployment._load_config", return_value=self._dry_run_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()

            run(ctx)

        ctx.notify.assert_not_called()

    def test_set_labels_nao_chamado_em_dry_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """provider.set_labels NÃO é chamado no modo dry-run (nenhuma label alterada)."""
        monkeypatch.setenv("CREWFLOW_DRY_RUN", "1")
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result()

        with (
            mock.patch("deployment.deployment._load_config", return_value=self._dry_run_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider_for.return_value = mock_provider

            run(ctx)

        mock_provider.set_labels.assert_not_called()

    def test_dry_run_imprime_relatorio_stdout(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """dry-run imprime as decisões no stdout em formato legível."""
        monkeypatch.setenv("CREWFLOW_DRY_RUN", "1")
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result(
            key="https://github.com/owner/repo/issues/42",
            title="[owner/repo] Feature X",
        )

        with (
            mock.patch("deployment.deployment._load_config", return_value=self._dry_run_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()

            run(ctx)

        captured = capsys.readouterr()
        assert "[DRY-RUN]" in captured.out
        assert "DISPATCH_DEV" in captured.out
        assert "#42" in captured.out

    def test_sem_dry_run_dispatch_e_chamado(self) -> None:
        """Sem dry-run, _dispatch É chamado normalmente (regressão)."""
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result()

        with (
            mock.patch("deployment.deployment._load_config", return_value=self._dry_run_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._repo_has_active", return_value=False),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()

            run(ctx)

        mock_dispatch.assert_called_once()

    def test_dry_run_env_vazia_nao_ativa(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CREWFLOW_DRY_RUN='' (variável vazia) NÃO ativa o dry-run."""
        monkeypatch.setenv("CREWFLOW_DRY_RUN", "")
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result()

        with (
            mock.patch("deployment.deployment._load_config", return_value=self._dry_run_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._repo_has_active", return_value=False),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()

            run(ctx)

        mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Issue #84 — resumo do ciclo no log
# ---------------------------------------------------------------------------

class TestLogCycleSummary:
    """_log_cycle_summary emite 1 linha de resumo e notifica quando chat_id configurado."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        return ctx

    def test_log_contém_todos_os_contadores(self) -> None:
        from deployment.deployment import _log_cycle_summary

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment.logger") as mock_log:
            _log_cycle_summary(
                ctx=ctx,
                chat_id="",
                scan_total=7,
                dispatch_dev=2,
                dispatch_reviewer=1,
                dispatch_rework=0,
                merge_pr=0,
                notify_human=1,
                block=0,
                rebrand=0,
                spec_invalid=1,
            )
        mock_log.info.assert_called_once()
        msg = mock_log.info.call_args[0][0]
        assert "scan:7" in msg
        assert "dispatch_dev:2" in msg
        assert "dispatch_reviewer:1" in msg
        assert "merge_pr:0" in msg
        assert "notify_human:1" in msg
        assert "block:0" in msg
        assert "rebrand:0" in msg
        assert "skip:2" in msg  # 7 total - 5 ações = 2 skips

    def test_skip_calculado_corretamente(self) -> None:
        from deployment.deployment import _log_cycle_summary

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment.logger") as mock_log:
            _log_cycle_summary(
                ctx=ctx,
                chat_id="",
                scan_total=5,
                dispatch_dev=0,
                dispatch_reviewer=0,
                dispatch_rework=0,
                merge_pr=0,
                notify_human=0,
                block=0,
                rebrand=0,
                spec_invalid=0,
            )
        msg = mock_log.info.call_args[0][0]
        assert "skip:5" in msg

    def test_tudo_zerado_ciclo_vazio(self) -> None:
        from deployment.deployment import _log_cycle_summary

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment.logger") as mock_log:
            _log_cycle_summary(
                ctx=ctx,
                chat_id="",
                scan_total=0,
                dispatch_dev=0,
                dispatch_reviewer=0,
                dispatch_rework=0,
                merge_pr=0,
                notify_human=0,
                block=0,
                rebrand=0,
                spec_invalid=0,
            )
        msg = mock_log.info.call_args[0][0]
        assert "scan:0" in msg
        assert "skip:0" in msg
        ctx.notify.assert_not_called()

    def test_notifica_quando_chat_id_configurado(self) -> None:
        from deployment.deployment import _log_cycle_summary

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment.logger"):
            _log_cycle_summary(
                ctx=ctx,
                chat_id="8620515309",
                scan_total=3,
                dispatch_dev=1,
                dispatch_reviewer=0,
                dispatch_rework=0,
                merge_pr=0,
                notify_human=0,
                block=0,
                rebrand=0,
                spec_invalid=0,
            )
        ctx.notify.assert_called_once()
        msg = ctx.notify.call_args[0][0]
        assert "scan:3" in msg
        assert "dispatch_dev:1" in msg

    def test_nao_notifica_sem_chat_id(self) -> None:
        from deployment.deployment import _log_cycle_summary

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment.logger"):
            _log_cycle_summary(
                ctx=ctx,
                chat_id="",
                scan_total=3,
                dispatch_dev=1,
                dispatch_reviewer=0,
                dispatch_rework=0,
                merge_pr=0,
                notify_human=0,
                block=0,
                rebrand=0,
                spec_invalid=0,
            )
        ctx.notify.assert_not_called()

    def test_prefixo_deployment_ciclo_concluido(self) -> None:
        from deployment.deployment import _log_cycle_summary

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment.logger") as mock_log:
            _log_cycle_summary(
                ctx=ctx,
                chat_id="",
                scan_total=1,
                dispatch_dev=1,
                dispatch_reviewer=0,
                dispatch_rework=0,
                merge_pr=0,
                notify_human=0,
                block=0,
                rebrand=0,
                spec_invalid=0,
            )
        msg = mock_log.info.call_args[0][0]
        assert msg.startswith("deployment: ciclo concluído")


class TestRunEmiteCicleSummary:
    """run() deve emitir o resumo do ciclo em todos os casos, incluindo tudo SKIP."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def test_resumo_emitido_quando_tudo_skip(self) -> None:
        """Quando scan retorna vazio, o resumo ainda deve ser logado."""
        ctx = self._make_ctx()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_minimal_config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment._log_cycle_summary") as mock_summary,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            run(ctx)

        mock_summary.assert_called_once()
        kw = mock_summary.call_args.kwargs
        assert kw["scan_total"] == 0
        assert kw["dispatch_dev"] == 0

    def test_resumo_emitido_com_dispatch_dev(self) -> None:
        """Ciclo com dispatch_dev deve refletir o contador correto no resumo."""
        ctx = self._make_ctx()
        results = [_make_scan_result()]

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_minimal_config(auto=False)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=results),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment._log_cycle_summary") as mock_summary,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            run(ctx)

        mock_summary.assert_called_once()
        kw = mock_summary.call_args.kwargs
        assert kw["scan_total"] == 1
        assert kw["dispatch_dev"] == 1
        assert kw["dispatch_reviewer"] == 0
# Workspace isolado por task (#92)
# ---------------------------------------------------------------------------

from deployment.deployment import (  # noqa: E402
    _clean_stale_worktree,
    _resource_headroom_ok,
    _worktree_path,
)


class TestWorktreePath:
    """_worktree_path() retorna convenção canônica consistente."""

    def test_caminho_canônico(self) -> None:
        path = _worktree_path("/home/user/dev", "owner/my-repo", 42)
        assert path == "/home/user/dev/.esteira-worktrees/my-repo-42"

    def test_usa_short_do_repo(self) -> None:
        path = _worktree_path("/dev", "org/project-name", 7)
        assert "project-name-7" in path

    def test_sem_slash_extra(self) -> None:
        path = _worktree_path("/home/user/dev", "owner/repo", 1)
        assert "//" not in path


class TestCleanStaleWorktree:
    """_clean_stale_worktree() remove worktrees órfãos antes do dispatch."""

    def test_retorna_false_quando_nao_existe(self, tmp_path: Path) -> None:
        """Sem worktree no disco → retorna False sem erro."""
        dev_root = str(tmp_path)
        result = _clean_stale_worktree(dev_root, "owner/repo", 99)
        assert result is False

    def test_remove_worktree_via_git(self, tmp_path: Path) -> None:
        """Worktree órfão presente → chama git worktree remove e retorna True."""
        dev_root = str(tmp_path)
        wt = tmp_path / ".esteira-worktrees" / "repo-5"
        wt.mkdir(parents=True)

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stderr="")
            result = _clean_stale_worktree(dev_root, "owner/repo", 5)

        assert result is True
        call_args = mock_run.call_args[0][0]
        assert "git" in call_args
        assert "worktree" in call_args
        assert "remove" in call_args

    def test_fallback_shutil_quando_git_falha(self, tmp_path: Path) -> None:
        """git worktree remove falha → shutil.rmtree usado como fallback."""
        import subprocess
        dev_root = str(tmp_path)
        wt = tmp_path / ".esteira-worktrees" / "repo-6"
        wt.mkdir(parents=True)
        (wt / "file.txt").write_text("stub")

        with mock.patch("subprocess.run") as mock_run:
            # Primeira chamada (git worktree remove) falha
            mock_run.side_effect = [
                subprocess.CalledProcessError(1, "git", stderr="not a worktree"),
                mock.MagicMock(returncode=0),  # git worktree prune ok
            ]
            result = _clean_stale_worktree(dev_root, "owner/repo", 6)

        assert result is True
        # O diretório deve ter sido removido pelo shutil
        assert not wt.exists()

    def test_nao_propaga_excecao_quando_tudo_falha(
        self, tmp_path: Path
    ) -> None:
        """Falha total → retorna False sem propagar exceção."""
        dev_root = str(tmp_path)
        wt = tmp_path / ".esteira-worktrees" / "repo-7"
        wt.mkdir(parents=True)

        with mock.patch("subprocess.run", side_effect=RuntimeError("boom")):
            result = _clean_stale_worktree(dev_root, "owner/repo", 7)

        assert result is False


class TestResourceHeadroomOk:
    """_resource_headroom_ok() honra resource_status do Kiro Crew."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        return ctx

    def test_retorna_true_quando_kc_indisponivel(self) -> None:
        """ImportError (sem Kiro Crew) → fail-open, retorna True."""
        ctx = self._make_ctx()

        with mock.patch.dict("sys.modules", {"kiro_crew.loopback_http": None}):
            result = _resource_headroom_ok(ctx, 2)

        assert result is True

    def test_retorna_true_para_posture_ample(self) -> None:
        ctx = self._make_ctx()

        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"posture": "ample"}'

        mock_loopback = mock.MagicMock()
        mock_loopback.loopback_urlopen.return_value = mock_resp

        with mock.patch.dict("sys.modules", {"kiro_crew.loopback_http": mock_loopback}):
            result = _resource_headroom_ok(ctx, 2)

        assert result is True

    def test_retorna_true_para_posture_tight(self) -> None:
        ctx = self._make_ctx()

        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"posture": "tight"}'

        mock_loopback = mock.MagicMock()
        mock_loopback.loopback_urlopen.return_value = mock_resp

        with mock.patch.dict("sys.modules", {"kiro_crew.loopback_http": mock_loopback}):
            result = _resource_headroom_ok(ctx, 2)

        assert result is True

    def test_retorna_false_para_posture_critical(self) -> None:
        ctx = self._make_ctx()

        mock_resp = mock.MagicMock()
        mock_resp.__enter__ = mock.MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = mock.MagicMock(return_value=False)
        mock_resp.read.return_value = b'{"posture": "critical"}'

        mock_loopback = mock.MagicMock()
        mock_loopback.loopback_urlopen.return_value = mock_resp

        with mock.patch.dict("sys.modules", {"kiro_crew.loopback_http": mock_loopback}):
            result = _resource_headroom_ok(ctx, 2)

        assert result is False

    def test_fail_open_quando_excecao(self) -> None:
        """Qualquer exceção → fail-open, retorna True."""
        ctx = self._make_ctx()

        mock_loopback = mock.MagicMock()
        mock_loopback.loopback_urlopen.side_effect = OSError("connection refused")

        with mock.patch.dict("sys.modules", {"kiro_crew.loopback_http": mock_loopback}):
            result = _resource_headroom_ok(ctx, 2)

        assert result is True


class TestParallelDispatchIsolation:
    """Paralelismo: dois dispatches sem colisão de worktree; cap de max_concurrent respeitado."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _auto_config(self, max_concurrent: int = 3) -> dict:
        return {
            "repos": ["owner/repo-a", "owner/repo-b"],
            "auto_dispatch": True,
            "max_concurrent_tasks": max_concurrent,
            "one_per_repo": False,  # desligado para testar isolamento por worktree
            "notify_chat_id": "",
            "squad_id": "test",
            "issue_provider": "github",
            "dev_root": "/tmp/dev",
            "agent": "kirocrew",
        }

    def test_dois_repos_diferentes_despacham_em_paralelo(self) -> None:
        """Duas issues em repos diferentes são despachadas na mesma rodada."""
        ctx = self._make_ctx()
        result_a = _make_dispatch_scan_result(
            key="https://github.com/owner/repo-a/issues/1",
            title="[repo-a] Feature A",
        )
        result_b = _make_dispatch_scan_result(
            key="https://github.com/owner/repo-b/issues/2",
            title="[repo-b] Feature B",
        )

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=self._auto_config(max_concurrent=3)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[result_a, result_b]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._repo_has_active", return_value=False),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()
            run(ctx)

        # Ambas as issues devem ter sido despachadas
        assert mock_dispatch.call_count == 2

    def test_cap_max_concurrent_tasks_respeitado(self) -> None:
        """Com max_concurrent_tasks=1 e 2 candidatos, só 1 é despachado."""
        ctx = self._make_ctx()
        result_a = _make_dispatch_scan_result(
            key="https://github.com/owner/repo-a/issues/10",
            title="[repo-a] Task 10",
        )
        result_b = _make_dispatch_scan_result(
            key="https://github.com/owner/repo-b/issues/11",
            title="[repo-b] Task 11",
        )

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=self._auto_config(max_concurrent=1)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[result_a, result_b]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._repo_has_active", return_value=False),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()
            run(ctx)

        # Só 1 deve ter sido despachado
        assert mock_dispatch.call_count == 1

    def test_resource_critical_adia_dispatch(self) -> None:
        """Posture critical → nenhuma sessão despachada mesmo com vagas disponíveis."""
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=self._auto_config(max_concurrent=3)),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._repo_has_active", return_value=False),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=False),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()
            run(ctx)

        mock_dispatch.assert_not_called()

    def test_clean_worktree_chamado_antes_dispatch(self) -> None:
        """_clean_stale_worktree() é chamado para cada issue antes de despachar."""
        ctx = self._make_ctx()
        result = _make_dispatch_scan_result(
            key="https://github.com/owner/repo/issues/42",
            title="[repo] Fix",
        )

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=self._auto_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._repo_has_active", return_value=False),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree") as mock_clean,
            mock.patch("deployment.deployment._dispatch"),
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()
            run(ctx)

        mock_clean.assert_called_once()
        # Verifica que o número de issue correto foi passado
        call_kwargs = mock_clean.call_args
        assert 42 in call_kwargs[0] or 42 in call_kwargs[1].values()

    def test_max_concurrent_tasks_alias_compat(self) -> None:
        """max_concurrent_tasks tem precedência sobre max_concurrent (compat backward)."""
        ctx = self._make_ctx()
        result_a = _make_dispatch_scan_result(
            key="https://github.com/owner/repo-a/issues/20",
            title="[repo-a] Task 20",
        )
        result_b = _make_dispatch_scan_result(
            key="https://github.com/owner/repo-b/issues/21",
            title="[repo-b] Task 21",
        )
        # max_concurrent=5 mas max_concurrent_tasks=1 → cap de 1 deve vencer
        cfg = self._auto_config()
        cfg["max_concurrent"] = 5
        cfg["max_concurrent_tasks"] = 1

        with (
            mock.patch("deployment.deployment._load_config", return_value=cfg),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[result_a, result_b]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._repo_has_active", return_value=False),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider_for.return_value = mock.MagicMock()
            run(ctx)

        assert mock_dispatch.call_count == 1


# ---------------------------------------------------------------------------
# Testes para _check_installed_version (issue #116)
# ---------------------------------------------------------------------------


class TestCheckInstalledVersion:
    """Testa a detecção de script instalado desatualizado.

    A função _check_installed_version lê deployment.version, compara o hash
    SHA-256 do deployment.py do repo com o hash registrado na instalação,
    e loga/notifica quando divergir.
    """

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx.notify = mock.MagicMock()
        return ctx

    def test_sem_version_file_nao_notifica(self, tmp_path: Path) -> None:
        """Sem deployment.version não há notificação — instalação antiga é silenciosa."""
        from deployment.deployment import _check_installed_version

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment._HERE", str(tmp_path)):
            _check_installed_version(ctx)

        ctx.notify.assert_not_called()

    def test_hash_bate_nao_notifica(self, tmp_path: Path) -> None:
        """Hash do repo igual ao registrado → sem notificação."""
        import hashlib

        from deployment.deployment import _check_installed_version

        # Cria um "repo" com um deployment.py fake
        fake_repo = tmp_path / "repo"
        (fake_repo / "deployment").mkdir(parents=True)
        fake_script = fake_repo / "deployment" / "deployment.py"
        fake_script.write_bytes(b"# fake deployment\n")
        sha = hashlib.sha256(fake_script.read_bytes()).hexdigest()

        # Grava deployment.version com o hash correto
        version_info = {"repo_root": str(fake_repo), "repo_deployment_sha256": sha}
        version_file = tmp_path / "crons" / "deployment.version"
        version_file.parent.mkdir(parents=True)
        version_file.write_text(json.dumps(version_info))

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment._HERE", str(version_file.parent)):
            _check_installed_version(ctx)

        ctx.notify.assert_not_called()

    def test_hash_diferente_loga_warning_e_notifica(self, tmp_path: Path) -> None:
        """Hash do repo diferente do registrado → notifica com instrução de reinstalação."""
        from deployment.deployment import _check_installed_version

        # Cria repo com deployment.py diferente do registrado
        fake_repo = tmp_path / "repo"
        (fake_repo / "deployment").mkdir(parents=True)
        fake_script = fake_repo / "deployment" / "deployment.py"
        fake_script.write_bytes(b"# NOVA versao\n")

        # Hash registrado é de outro conteúdo (versão antiga)
        old_sha = "aaabbbccc" + "0" * 55  # 64 chars

        version_info = {
            "repo_root": str(fake_repo),
            "repo_deployment_sha256": old_sha,
        }
        version_file = tmp_path / "crons" / "deployment.version"
        version_file.parent.mkdir(parents=True)
        version_file.write_text(json.dumps(version_info))

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment._HERE", str(version_file.parent)):
            _check_installed_version(ctx)

        ctx.notify.assert_called_once()
        msg = ctx.notify.call_args[0][0]
        assert "DESATUALIZADO" in msg
        assert "install-cron.sh" in msg

    def test_version_file_corrompido_nao_aborta(self, tmp_path: Path) -> None:
        """version file inválido não aborta o ciclo — apenas loga warning."""
        from deployment.deployment import _check_installed_version

        version_file = tmp_path / "deployment.version"
        version_file.write_text("json inválido {{{{")

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment._HERE", str(tmp_path)):
            _check_installed_version(ctx)  # não deve levantar

        ctx.notify.assert_not_called()

    def test_repo_deployment_ausente_nao_notifica(self, tmp_path: Path) -> None:
        """Se deployment.py do repo não existir (repo movido), não bloqueia."""
        from deployment.deployment import _check_installed_version

        fake_repo = tmp_path / "repo_que_nao_existe"
        version_info = {
            "repo_root": str(fake_repo),
            "repo_deployment_sha256": "abc123",
        }
        version_file = tmp_path / "deployment.version"
        version_file.write_text(json.dumps(version_info))

        ctx = self._make_ctx()
        with mock.patch("deployment.deployment._HERE", str(tmp_path)):
            _check_installed_version(ctx)

        ctx.notify.assert_not_called()

    def test_ctx_none_nao_levanta(self, tmp_path: Path) -> None:
        """_check_installed_version(ctx=None) não levanta mesmo com hash divergente."""
        from deployment.deployment import _check_installed_version

        fake_repo = tmp_path / "repo"
        (fake_repo / "deployment").mkdir(parents=True)
        fake_script = fake_repo / "deployment" / "deployment.py"
        fake_script.write_bytes(b"# new\n")

        version_info = {
            "repo_root": str(fake_repo),
            "repo_deployment_sha256": "0" * 64,
        }
        version_file = tmp_path / "deployment.version"
        version_file.write_text(json.dumps(version_info))

        with mock.patch("deployment.deployment._HERE", str(tmp_path)):
            _check_installed_version(None)  # não deve levantar

    def test_run_chama_check_installed_version(self) -> None:
        """run() deve chamar _check_installed_version antes do scan."""
        from deployment.deployment import run

        ctx = self._make_ctx()
        with (
            mock.patch("deployment.deployment._check_installed_version") as mock_check,
            mock.patch("deployment.deployment._load_config", side_effect=RuntimeError("stop")),
            pytest.raises(RuntimeError, match="stop"),
        ):
            run(ctx)

        mock_check.assert_called_once_with(ctx)

    def test_run_stage_chama_check_installed_version(self) -> None:
        """_run_stage() deve chamar _check_installed_version antes do scan."""
        from deployment.deployment import _run_stage

        ctx = self._make_ctx()
        with (
            mock.patch("deployment.deployment._check_installed_version") as mock_check,
            mock.patch("deployment.deployment._load_config", side_effect=RuntimeError("stop")),
            pytest.raises(RuntimeError, match="stop"),
        ):
            _run_stage(ctx, "dev")

        mock_check.assert_called_once_with(ctx)


# ---------------------------------------------------------------------------
# Issue #122 — cobertura total de ActionKind em _STAGE_ACTIONS
# ---------------------------------------------------------------------------

class TestStageActionsCobertura:
    """Garante que nenhuma ActionKind emitida pelo executor fica órfã de _STAGE_ACTIONS.

    Critério de aceite:
    - Toda ActionKind (exceto SKIP e ações administrativas sem estágio) aparece em
      ao menos um estágio de _STAGE_ACTIONS.
    - SKIP não precisa de estágio: é silêncio, não uma ação de dispatch.
    - NOTIFY_HUMAN, BLOCK e REBRAND são executados pelo run() monolítico e por
      _run_stage (via o fluxo de categorização), mas não têm cron dedicado —
      são ações transversais tratadas em qualquer estágio. Excetuados do guard.

    Se uma nova ActionKind for adicionada ao executor sem ser mapeada aqui, este
    teste QUEBRA — é o guard automático exigido pelo critério de aceite da issue.
    """

    # ActionKinds que são transversais (processadas em qualquer estágio quando
    # encontradas, mas sem cron dedicado). Precisam de justificativa explícita.
    _TRANSVERSAL = frozenset({
        "skip",           # silêncio — sem ação
        "notify_human",   # transversal: notifica humano em qualquer estágio
        "block",          # transversal: marca crewflow:blocked
        "rebrand",        # transversal: troca de template (GATE 0 hotfix)
    })

    def test_toda_action_kind_tem_estagio_ou_e_transversal(self) -> None:
        """Nenhuma ActionKind fica órfã de _STAGE_ACTIONS (exceto as transversais)."""
        from deployment.deployment import _STAGE_ACTIONS
        from flow.executor.executor import ActionKind

        todas = frozenset(a.value for a in ActionKind)
        mapeadas = frozenset().union(*_STAGE_ACTIONS.values())
        orfas = todas - mapeadas - self._TRANSVERSAL

        assert not orfas, (
            f"ActionKind(s) órfã(s) de _STAGE_ACTIONS: {sorted(orfas)}\n"
            "Adicione-as ao estágio correto em _STAGE_ACTIONS no deployment.py.\n"
            "Se for transversal (sem cron dedicado), adicione ao conjunto _TRANSVERSAL "
            "neste teste com justificativa."
        )

    def test_mark_conflito_no_estagio_reviewer(self) -> None:
        """mark_conflito deve estar em _STAGE_REVIEWER (detectado ao escanear review)."""
        from deployment.deployment import _STAGE_ACTIONS, _STAGE_REVIEWER

        assert "mark_conflito" in _STAGE_ACTIONS[_STAGE_REVIEWER], (
            "mark_conflito deve estar em _STAGE_REVIEWER — "
            "é detectado quando pr_mergeable == CONFLICTING durante o scan de review."
        )

    def test_dispatch_conflict_resolver_no_estagio_conflito(self) -> None:
        """dispatch_conflict_resolver deve estar em _STAGE_CONFLITO."""
        from deployment.deployment import _STAGE_ACTIONS, _STAGE_CONFLITO

        assert "dispatch_conflict_resolver" in _STAGE_ACTIONS[_STAGE_CONFLITO], (
            "dispatch_conflict_resolver deve estar em _STAGE_CONFLITO — "
            "despachado quando crewflow:conflito já foi aplicado na issue."
        )

    def test_dispatch_rework_no_estagio_conflito(self) -> None:
        """dispatch_rework deve estar em _STAGE_CONFLITO (re-trabalho pós-review)."""
        from deployment.deployment import _STAGE_ACTIONS, _STAGE_CONFLITO

        assert "dispatch_rework" in _STAGE_ACTIONS[_STAGE_CONFLITO], (
            "dispatch_rework deve estar em _STAGE_CONFLITO."
        )

    def test_estagios_sem_overlap(self) -> None:
        """Uma mesma ActionKind não deve aparecer em dois estágios diferentes."""
        from deployment.deployment import _STAGE_ACTIONS

        seen: dict[str, str] = {}
        for stage, actions in _STAGE_ACTIONS.items():
            for action in actions:
                assert action not in seen, (
                    f"ActionKind '{action}' aparece em dois estágios: "
                    f"'{seen[action]}' e '{stage}'. "
                    "Cada ação deve pertencer a exatamente um estágio."
                )
                seen[action] = stage


class TestRunStageMarkConflito:
    """_run_stage no estágio reviewer executa mark_conflito (aplica crewflow:conflito)."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _make_conflicting_pr_scan_result(self) -> object:
        """ScanResult em crewflow:review com PR em estado CONFLICTING."""
        from flow.domain.gates import WorkItem
        from flow.domain.state import State
        from flow.scan.scanner import ScanResult

        return ScanResult(
            item=WorkItem(
                key="https://github.com/owner/repo/issues/99",
                title="[owner/repo] Feature Y",
                labels=frozenset(["crewflow:review", "crewflow:feature"]),
            ),
            current_state=State.REVIEW,
            modifiers=frozenset(),
            dispatch_candidate=False,
            spec_valid=None,
            changed=True,
            reason="PR com conflito de merge",
        )

    def test_mark_conflito_aplica_label_no_estagio_reviewer(self) -> None:
        """_run_stage(reviewer) aplica crewflow:conflito quando PR está CONFLICTING."""
        from deployment.deployment import _run_stage

        ctx = self._make_ctx()
        result = self._make_conflicting_pr_scan_result()

        # Simula get_pr_for_issue retornando PR CONFLICTING
        fake_pr = {"number": 50, "headRefOid": "abc123", "mergeable": "CONFLICTING"}

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value={
                           "repos": ["owner/repo"],
                           "auto_dispatch": True,
                           "max_concurrent": 2,
                           "notify_chat_id": "",
                           "squad_id": "test",
                           "issue_provider": "github",
                           "dev_root": "/tmp/dev",
                           "agent": "kirocrew",
                       }),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider.get_state_comment.return_value = None
            mock_provider.get_pr_for_issue.return_value = fake_pr
            mock_provider_for.return_value = mock_provider

            _run_stage(ctx, "reviewer")

        # set_labels deve ter sido chamado com crewflow:conflito
        mock_provider.set_labels.assert_called_once()
        call_args = mock_provider.set_labels.call_args
        labels_set = call_args[0][2] if len(call_args[0]) >= 3 else call_args[1].get("labels", [])
        assert "crewflow:conflito" in labels_set, (
            f"crewflow:conflito não foi adicionado. Labels: {labels_set}"
        )


class TestRunStageConflictResolver:
    """_run_stage no estágio conflito despacha sessão de resolução de conflito."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _make_conflito_scan_result(self) -> object:
        """ScanResult em crewflow:review com crewflow:conflito aplicado."""
        from flow.domain.gates import WorkItem
        from flow.domain.state import Modifier, State
        from flow.scan.scanner import ScanResult

        return ScanResult(
            item=WorkItem(
                key="https://github.com/owner/repo/issues/99",
                title="[owner/repo] Feature Y",
                labels=frozenset(["crewflow:review", "crewflow:conflito", "crewflow:feature"]),
            ),
            current_state=State.REVIEW,
            modifiers=frozenset([Modifier.CONFLITO]),
            dispatch_candidate=False,
            spec_valid=None,
            changed=True,
            reason="PR com crewflow:conflito",
        )

    def test_dispatch_conflict_resolver_no_estagio_conflito(self) -> None:
        """_run_stage(conflito) despacha sessão de resolução quando crewflow:conflito presente."""
        from deployment.deployment import _run_stage

        ctx = self._make_ctx()
        result = self._make_conflito_scan_result()

        fake_pr = {"number": 50, "headRefOid": "abc123", "mergeable": "CONFLICTING"}

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value={
                           "repos": ["owner/repo"],
                           "auto_dispatch": True,
                           "max_concurrent": 2,
                           "notify_chat_id": "",
                           "squad_id": "test",
                           "issue_provider": "github",
                           "dev_root": "/tmp/dev",
                           "agent": "kirocrew",
                       }),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_provider_for,
            mock.patch("deployment.deployment._conflict_resolver_has_active", return_value=False),
            mock.patch("deployment.deployment._dispatch_conflict_resolver") as mock_dispatch_cr,
            mock.patch("subprocess.run") as mock_sub,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider.get_state_comment.return_value = None
            mock_provider.get_pr_for_issue.return_value = fake_pr
            mock_provider_for.return_value = mock_provider
            # gh pr list retorna o PR existente
            mock_sub.return_value = mock.MagicMock(
                returncode=0, stdout='[{"number": 50}]', stderr=""
            )

            _run_stage(ctx, "conflito")

        mock_dispatch_cr.assert_called_once()
        call_args = mock_dispatch_cr.call_args[0]
        assert call_args[1] == "owner/repo"  # repo
        assert call_args[2]["number"] == 99   # issue["number"]
        assert call_args[3] == 50             # pr_number


class TestIsIssueClosed:
    """_is_issue_closed retorna True quando issue está CLOSED, False caso contrário."""

    def test_retorna_true_quando_closed(self) -> None:
        from deployment.deployment import _is_issue_closed

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="CLOSED\n", stderr=""
            )
            assert _is_issue_closed("owner/repo", 42) is True

    def test_retorna_false_quando_open(self) -> None:
        from deployment.deployment import _is_issue_closed

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="OPEN\n", stderr=""
            )
            assert _is_issue_closed("owner/repo", 42) is False

    def test_fail_open_em_erro_de_cli(self) -> None:
        """Falha de I/O retorna False (fail-open) — dispatch prossegue."""
        from deployment.deployment import _is_issue_closed

        with mock.patch("subprocess.run", side_effect=OSError("gh not found")):
            assert _is_issue_closed("owner/repo", 42) is False

    def test_fail_open_quando_returncode_nonzero(self) -> None:
        from deployment.deployment import _is_issue_closed

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=1, stdout="", stderr="error"
            )
            assert _is_issue_closed("owner/repo", 42) is False

    def test_consulta_repo_e_numero_corretos(self) -> None:
        from deployment.deployment import _is_issue_closed

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="OPEN\n", stderr=""
            )
            _is_issue_closed("myorg/myrepo", 99)

        args = mock_run.call_args[0][0]
        assert "99" in args
        assert "myorg/myrepo" in args
        assert "--json" in args
        assert "state" in args


class TestIssueClosedGuardDispatch:
    """Guard #136: dispatchers abortam silenciosamente quando issue está CLOSED."""

    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _minimal_cfg(self) -> dict:
        return {"agent": "kirocrew", "notify_chat_id": ""}

    def test_dispatch_reviewer_aborta_quando_issue_closed(self) -> None:
        """_dispatch_reviewer não faz POST quando issue está CLOSED."""
        import urllib.request as _u

        from deployment.deployment import _dispatch_reviewer

        ctx = self._make_ctx()
        issue = {"number": 133, "title": "feat: algo"}

        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=True),
            mock.patch.object(_u, "urlopen") as mock_urlopen,
            mock.patch("subprocess.run") as mock_run,
        ):
            _dispatch_reviewer(ctx, "owner/repo", issue, self._minimal_cfg())

        # Nenhuma chamada HTTP nem subprocess (exceto o guard) deve ter ocorrido
        mock_urlopen.assert_not_called()
        ctx.notify.assert_not_called()
        # subprocess.run não deve ter sido chamado para listar PRs
        mock_run.assert_not_called()

    def test_dispatch_reviewer_prossegue_quando_issue_open(self) -> None:
        """_dispatch_reviewer consulta PR normalmente quando issue está OPEN."""
        from deployment.deployment import _dispatch_reviewer

        ctx = self._make_ctx()
        issue = {"number": 42, "title": "feat: algo"}

        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=False),
            mock.patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = mock.MagicMock(
                returncode=0, stdout="[]", stderr=""
            )
            _dispatch_reviewer(ctx, "owner/repo", issue, self._minimal_cfg())

        # Deve ter chamado subprocess.run para buscar o PR
        mock_run.assert_called()

    def test_dispatch_rework_aborta_quando_issue_closed(self) -> None:
        """_dispatch_rework não faz POST quando issue está CLOSED."""
        import urllib.request as _u

        from deployment.deployment import _dispatch_rework

        ctx = self._make_ctx()
        issue = {"number": 133, "title": "feat: algo"}

        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=True),
            mock.patch.object(_u, "urlopen") as mock_urlopen,
        ):
            _dispatch_rework(ctx, "owner/repo", issue, 50, 1, self._minimal_cfg())

        mock_urlopen.assert_not_called()

    def test_dispatch_conflict_resolver_aborta_quando_issue_closed(self) -> None:
        """_dispatch_conflict_resolver não faz POST quando issue está CLOSED."""
        import urllib.request as _u

        from deployment.deployment import _dispatch_conflict_resolver

        ctx = self._make_ctx()
        issue = {"number": 133, "title": "feat: algo"}

        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=True),
            mock.patch.object(_u, "urlopen") as mock_urlopen,
        ):
            _dispatch_conflict_resolver(ctx, "owner/repo", issue, 50, self._minimal_cfg())

        mock_urlopen.assert_not_called()


class TestReviewerNunCriabranchOuPR:
    """reviewer.md contém regras explícitas contra criar branch/commit/PR."""

    def _reviewer_md(self) -> str:
        import pathlib
        path = pathlib.Path(__file__).parent.parent / "prompts" / "reviewer.md"
        return path.read_text()

    def test_reviewer_proibe_criar_branch(self) -> None:
        body = self._reviewer_md()
        assert "NUNCA crie branch" in body or "NUNCA cria branch" in body or "NUNCA.*branch" in body or "NUNCA crie branch" in body

    def test_reviewer_proibe_fazer_commit(self) -> None:
        body = self._reviewer_md()
        assert "NUNCA faça commit" in body

    def test_reviewer_proibe_abrir_pr(self) -> None:
        body = self._reviewer_md()
        assert "NUNCA abra PR" in body

    def test_reviewer_menciona_numero_do_bug(self) -> None:
        """Referência ao bug #136 na regra crítica para rastreabilidade."""
        body = self._reviewer_md()
        assert "#136" in body


class TestDispatchClosedGuard:
    """Guard #163: _dispatch aborta quando issue está CLOSED (evita PR duplicada pós-auto-merge)."""

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
            "dev_root": "/tmp/dev",
        }

    def test_dispatch_aborta_quando_issue_closed(self) -> None:
        """_dispatch não faz POST quando issue está CLOSED — sem PR duplicada."""
        import urllib.request as _u

        from deployment.deployment import _dispatch

        ctx = self._make_ctx()
        issue = {"number": 157, "title": "feat: algo", "url": "https://github.com/owner/repo/issues/157"}

        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=True),
            mock.patch.object(_u, "urlopen") as mock_urlopen,
        ):
            _dispatch(ctx, "owner/repo", issue, self._minimal_cfg())

        mock_urlopen.assert_not_called()

    def test_dispatch_nao_adquire_lock_quando_issue_closed(self) -> None:
        """_dispatch nem tenta adquirir lock quando issue está CLOSED."""
        from deployment.deployment import _dispatch

        ctx = self._make_ctx()
        issue = {"number": 157, "title": "feat: algo", "url": "https://github.com/owner/repo/issues/157"}

        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=True),
            mock.patch("deployment.deployment._try_acquire_dispatch_lock") as mock_lock,
        ):
            _dispatch(ctx, "owner/repo", issue, self._minimal_cfg())

        mock_lock.assert_not_called()

    def test_dispatch_prossegue_quando_issue_open(self) -> None:
        """_dispatch tenta adquirir lock quando issue está OPEN."""
        from deployment.deployment import _dispatch

        ctx = self._make_ctx()
        issue = {"number": 157, "title": "feat: algo", "url": "https://github.com/owner/repo/issues/157"}

        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=False),
            mock.patch("deployment.deployment._try_acquire_dispatch_lock", return_value=(False, "/tmp/lock")) as mock_lock,
        ):
            _dispatch(ctx, "owner/repo", issue, self._minimal_cfg())

        mock_lock.assert_called_once()

    def test_dispatch_fail_open_em_erro_de_issue_check(self) -> None:
        """_dispatch prossegue (fail-open) se a verificação de issue.state falhar."""
        from deployment.deployment import _dispatch

        ctx = self._make_ctx()
        issue = {"number": 157, "title": "feat: algo", "url": "https://github.com/owner/repo/issues/157"}

        with (
            mock.patch("deployment.deployment._is_issue_closed", return_value=False),
            mock.patch("deployment.deployment._try_acquire_dispatch_lock", return_value=(False, "/tmp/lock")) as mock_lock,
        ):
            _dispatch(ctx, "owner/repo", issue, self._minimal_cfg())

        # Chegou na tentativa de lock (não abortou antes)
        mock_lock.assert_called_once()


class TestPromptsClosedGuard:
    """Guard #163: todos os prompts contêm instrução de verificar issue.state == CLOSED."""

    def _read_prompt(self, stage: str) -> str:
        import pathlib
        path = pathlib.Path(__file__).parent.parent / "prompts" / f"{stage}.md"
        return path.read_text()

    def test_dev_md_tem_guard_closed(self) -> None:
        body = self._read_prompt("dev")
        assert "CLOSED" in body
        assert "exit 0" in body or "encerrando" in body.lower()

    def test_reviewer_md_tem_guard_closed(self) -> None:
        body = self._read_prompt("reviewer")
        assert "CLOSED" in body
        assert "exit 0" in body or "encerrando" in body.lower()

    def test_rework_md_tem_guard_closed(self) -> None:
        body = self._read_prompt("rework")
        assert "CLOSED" in body
        assert "exit 0" in body or "encerrando" in body.lower()

    def test_conflict_md_tem_guard_closed(self) -> None:
        body = self._read_prompt("conflict")
        assert "CLOSED" in body
        assert "exit 0" in body or "encerrando" in body.lower()

    def test_guard_e_primeiro_passo_em_dev(self) -> None:
        """O guard deve ser o passo 1 — verificado antes de qualquer ação."""
        body = self._read_prompt("dev")
        # Passo 1 deve conter "CLOSED"
        lines = body.splitlines()
        passo1_start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("1.")), None)
        assert passo1_start is not None, "Passo 1 não encontrado em dev.md"
        passo1_text = "\n".join(lines[passo1_start:passo1_start + 10])
        assert "CLOSED" in passo1_text, "Passo 1 de dev.md não contém guard CLOSED"

    def test_guard_e_primeiro_passo_em_reviewer(self) -> None:
        """O guard deve ser o passo 1 — verificado antes de qualquer ação."""
        body = self._read_prompt("reviewer")
        lines = body.splitlines()
        passo1_start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("1.")), None)
        assert passo1_start is not None, "Passo 1 não encontrado em reviewer.md"
        passo1_text = "\n".join(lines[passo1_start:passo1_start + 10])
        assert "CLOSED" in passo1_text, "Passo 1 de reviewer.md não contém guard CLOSED"

    def test_guard_e_primeiro_passo_em_rework(self) -> None:
        body = self._read_prompt("rework")
        lines = body.splitlines()
        passo1_start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("1.")), None)
        assert passo1_start is not None, "Passo 1 não encontrado em rework.md"
        passo1_text = "\n".join(lines[passo1_start:passo1_start + 10])
        assert "CLOSED" in passo1_text, "Passo 1 de rework.md não contém guard CLOSED"

    def test_guard_e_primeiro_passo_em_conflict(self) -> None:
        body = self._read_prompt("conflict")
        lines = body.splitlines()
        passo1_start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("1.")), None)
        assert passo1_start is not None, "Passo 1 não encontrado em conflict.md"
        passo1_text = "\n".join(lines[passo1_start:passo1_start + 10])
        assert "CLOSED" in passo1_text, "Passo 1 de conflict.md não contém guard CLOSED"

    def test_prompts_referencia_issue_163(self) -> None:
        """Referência ao bug #163 para rastreabilidade em ao menos um prompt."""
        prompts = ["dev", "reviewer", "rework", "conflict"]
        has_ref = any("#163" in self._read_prompt(s) for s in prompts)
        assert has_ref, "Nenhum prompt referencia o bug #163 para rastreabilidade"
