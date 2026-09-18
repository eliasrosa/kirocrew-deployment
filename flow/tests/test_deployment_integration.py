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

from deployment.deployment import _scan_result_to_issue, run  # noqa: E402

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
