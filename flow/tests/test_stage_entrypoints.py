"""Testes para os entrypoints por estágio (issue #87).

Valida que cada cron por estágio (run_dev, run_reviewer, run_merge, run_conflito)
processa apenas as ações do seu estágio, ignorando issues dos demais.

Critérios de aceite da issue:
  - N crons independentes, cada um cobrindo um estágio
  - Cada cron aceita modelo próprio via config (stage_models)
  - Logs separados por estágio (prefixo deployment[stage]:)
  - Testes cobrindo o roteamento de cada estágio isolado
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest import mock

_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from deployment.deployment import (  # noqa: E402
    _STAGE_DEV,
    _STAGE_REVIEWER,
    _stage_model,
    run_conflito,
    run_dev,
    run_merge,
    run_reviewer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx() -> mock.MagicMock:
    ctx = mock.MagicMock()
    ctx._port = 5000
    ctx._secret = "secret"
    ctx.job.id = "test-job"
    return ctx


def _base_config(**overrides: object) -> dict:
    cfg: dict = {
        "repos": ["owner/repo"],
        "auto_dispatch": True,
        "max_concurrent_tasks": 2,
        "one_per_repo": False,
        "notify_chat_id": "",
        "squad_id": "test",
        "issue_provider": "github",
        "dev_root": "/tmp/dev",
        "agent": "kirocrew",
    }
    cfg.update(overrides)
    return cfg


def _make_scan_result(state_label: str, modifiers: list[str] | None = None) -> object:
    """Cria um ScanResult mínimo para o estado e modificadores dados."""
    from flow.domain.gates import WorkItem
    from flow.domain.state import State, parse_modifiers, parse_state
    from flow.scan.scanner import ScanResult

    labels_set = frozenset([state_label] + (modifiers or []))
    state = parse_state(labels_set)
    mods = parse_modifiers(labels_set)

    return ScanResult(
        item=WorkItem(
            key="https://github.com/owner/repo/issues/42",
            title="[owner/repo] Test issue",
            labels=labels_set,
        ),
        current_state=state,
        modifiers=mods,
        dispatch_candidate=(state is State.TODO and not any(
            m in labels_set for m in ("crewflow:blocked", "crewflow:running")
        )),
        spec_valid=None,
        changed=True,
        reason="test",
    )


# ---------------------------------------------------------------------------
# _stage_model — lê modelo do config por estágio
# ---------------------------------------------------------------------------

class TestStageModel:
    def test_retorna_none_quando_sem_stage_models(self) -> None:
        cfg = _base_config()
        assert _stage_model(cfg, _STAGE_DEV) is None

    def test_retorna_modelo_configurado(self) -> None:
        cfg = _base_config(stage_models={"dev": "sonnet-4-5", "reviewer": "haiku"})
        assert _stage_model(cfg, _STAGE_DEV) == "sonnet-4-5"
        assert _stage_model(cfg, _STAGE_REVIEWER) == "haiku"

    def test_retorna_none_para_estagio_nao_configurado(self) -> None:
        cfg = _base_config(stage_models={"dev": "sonnet-4-5"})
        assert _stage_model(cfg, _STAGE_REVIEWER) is None

    def test_stage_models_vazio_retorna_none(self) -> None:
        cfg = _base_config(stage_models={})
        assert _stage_model(cfg, _STAGE_DEV) is None


# ---------------------------------------------------------------------------
# run_dev — só despacha DISPATCH_DEV (issues crewflow:todo)
# ---------------------------------------------------------------------------

class TestRunDev:
    def test_despacha_issue_em_todo(self) -> None:
        """run_dev chama _dispatch para issue em crewflow:todo."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:todo")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_called_once()

    def test_nao_despacha_issue_em_review(self) -> None:
        """run_dev NÃO toca em issues crewflow:review (pertence ao cron reviewer)."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:review")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
            mock.patch("deployment.deployment._dispatch_reviewer") as mock_rev,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_not_called()
        mock_rev.assert_not_called()

    def test_nao_despacha_quando_auto_false(self) -> None:
        """run_dev com auto_dispatch=false notifica sem despachar."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:todo")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(auto_dispatch=False)),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_not_called()
        ctx.notify.assert_called()
        msg = ctx.notify.call_args[0][0]
        assert "Fase 1" in msg or "pronta" in msg

    def test_usa_modelo_do_stage_models(self) -> None:
        """run_dev substitui cfg['agent'] pelo modelo configurado em stage_models.dev."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:todo")

        capturado: list[dict] = []

        def _fake_dispatch(ctx: object, repo: str, issue: dict, cfg: dict, **kw: object) -> None:
            capturado.append({"agent": cfg.get("agent")})

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(stage_models={"dev": "modelo-forte"})),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch", side_effect=_fake_dispatch),
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        assert len(capturado) == 1
        assert capturado[0]["agent"] == "modelo-forte"

    def test_sem_stage_models_usa_agent_padrao(self) -> None:
        """run_dev sem stage_models.dev usa cfg['agent'] padrão."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:todo")

        capturado: list[dict] = []

        def _fake_dispatch(ctx: object, repo: str, issue: dict, cfg: dict, **kw: object) -> None:
            capturado.append({"agent": cfg.get("agent")})

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(agent="agente-padrao")),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch", side_effect=_fake_dispatch),
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        assert len(capturado) == 1
        assert capturado[0]["agent"] == "agente-padrao"


# ---------------------------------------------------------------------------
# run_reviewer — só despacha DISPATCH_REVIEWER (issues crewflow:review)
# ---------------------------------------------------------------------------

class TestRunReviewer:
    def test_despacha_review_sem_reviewed(self) -> None:
        """run_reviewer chama _dispatch_reviewer para issue em crewflow:review sem reviewed."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:review")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._reviewer_has_active", return_value=False),
            mock.patch("deployment.deployment._dispatch_reviewer") as mock_rev,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_reviewer(ctx)

        mock_rev.assert_called_once()

    def test_nao_despacha_issue_em_todo(self) -> None:
        """run_reviewer NÃO toca em issues crewflow:todo (pertence ao cron dev)."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:todo")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch_reviewer") as mock_rev,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_reviewer(ctx)

        mock_rev.assert_not_called()
        mock_dispatch.assert_not_called()

    def test_usa_modelo_do_stage_models_reviewer(self) -> None:
        """run_reviewer usa stage_models.reviewer para o dispatch."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:review")

        capturado: list[dict] = []

        def _fake_reviewer(
            ctx: object, repo: str, issue: dict, cfg: dict
        ) -> None:
            capturado.append({"agent": cfg.get("agent")})

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(stage_models={"reviewer": "haiku"})),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._reviewer_has_active", return_value=False),
            mock.patch("deployment.deployment._dispatch_reviewer",
                       side_effect=_fake_reviewer),
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_reviewer(ctx)

        assert len(capturado) == 1
        assert capturado[0]["agent"] == "haiku"

    def test_nao_despacha_quando_reviewer_ja_ativo(self) -> None:
        """run_reviewer não duplica dispatch quando sessão já está ativa."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:review")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._reviewer_has_active", return_value=True),
            mock.patch("deployment.deployment._dispatch_reviewer") as mock_rev,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_reviewer(ctx)

        mock_rev.assert_not_called()


# ---------------------------------------------------------------------------
# run_merge — só executa MERGE_PR
# ---------------------------------------------------------------------------

class TestRunMerge:
    def _make_merge_result(self) -> object:
        """ScanResult em crewflow:review + crewflow:reviewed com ReviewerResult aprovado."""
        from flow.audit.state_comment import StateComment, render
        from flow.domain.gates import WorkItem
        from flow.domain.state import Modifier, State
        from flow.scan.scanner import ScanResult

        sc = StateComment(
            workflow="feature (v1)", current_node="review",
            status="reviewed", repo="owner/repo",
        )
        sc.set_reviewer_result(approved=True, comments=[], sha="abc123")
        self._state_body = render(sc)

        return ScanResult(
            item=WorkItem(
                key="https://github.com/owner/repo/issues/42",
                title="[owner/repo] Feature X",
                labels=frozenset(["crewflow:review", "crewflow:reviewed"]),
            ),
            current_state=State.REVIEW,
            modifiers=frozenset([Modifier.REVIEWED]),
            dispatch_candidate=False,
            spec_valid=None,
            changed=True,
            reason="reviewer aprovado",
        )

    def test_executa_merge_quando_reviewer_aprovado(self) -> None:
        """run_merge executa _execute_auto_merges para PR aprovado com auto_merge_on_approve=true."""
        ctx = _make_ctx()
        result = self._make_merge_result()

        def _fake_get_state(repo: str, key: str) -> str:
            return self._state_body

        fake_pr = {
            "number": 99, "headRefName": "feat/issue-42",
            "headRefOid": "abc123", "body": "Closes #42",
        }
        cfg_with_auto_merge = _base_config(
            workflow_params={"auto_merge_on_approve": True}
        )

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=cfg_with_auto_merge),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._execute_auto_merges") as mock_merge,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider.get_state_comment = _fake_get_state
            mock_provider.get_pr_for_issue = mock.MagicMock(return_value=fake_pr)
            mock_pf.return_value = mock_provider
            run_merge(ctx)

        mock_merge.assert_called_once()

    def test_nao_executa_merge_para_issue_em_todo(self) -> None:
        """run_merge NÃO toca em issues crewflow:todo."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:todo")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._execute_auto_merges") as mock_merge,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_merge(ctx)

        mock_merge.assert_not_called()


# ---------------------------------------------------------------------------
# run_conflito — só despacha DISPATCH_REWORK (crewflow:changes-requested)
# ---------------------------------------------------------------------------

class TestRunConflito:
    def _make_rework_result(self) -> object:
        """ScanResult em crewflow:review + crewflow:changes-requested."""
        from flow.domain.gates import WorkItem
        from flow.domain.state import Modifier, State
        from flow.scan.scanner import ScanResult

        return ScanResult(
            item=WorkItem(
                key="https://github.com/owner/repo/issues/42",
                title="[owner/repo] Feature com mudanças",
                labels=frozenset(["crewflow:review", "crewflow:changes-requested"]),
            ),
            current_state=State.REVIEW,
            modifiers=frozenset([Modifier.CHANGES_REQUESTED]),
            dispatch_candidate=False,
            spec_valid=None,
            changed=True,
            reason="changes-requested",
        )

    def test_despacha_rework_para_changes_requested(self) -> None:
        """run_conflito chama _dispatch_rework para issue com changes-requested."""
        ctx = _make_ctx()
        result = self._make_rework_result()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._rework_has_active", return_value=False),
            mock.patch("deployment.deployment._dispatch_rework") as mock_rework,
            mock.patch("subprocess.run") as mock_sub,
        ):
            mock_sub.return_value = mock.MagicMock(
                returncode=0, stdout='[{"number": 55}]', stderr=""
            )
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_conflito(ctx)

        mock_rework.assert_called_once()

    def test_nao_despacha_para_issue_em_todo(self) -> None:
        """run_conflito NÃO toca em issues crewflow:todo."""
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:todo")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch_rework") as mock_rework,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_conflito(ctx)

        mock_rework.assert_not_called()
        mock_dispatch.assert_not_called()

    def test_notifica_sem_dispatch_quando_auto_false(self) -> None:
        """run_conflito com auto_dispatch=false notifica sem despachar rework."""
        ctx = _make_ctx()
        result = self._make_rework_result()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(auto_dispatch=False)),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch_rework") as mock_rework,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_conflito(ctx)

        mock_rework.assert_not_called()
        ctx.notify.assert_called()
        msg = ctx.notify.call_args[0][0]
        assert "re-trabalho" in msg.lower() or "mudanças" in msg.lower() or "conflito" in msg.lower()

    def test_usa_modelo_do_stage_models_conflito(self) -> None:
        """run_conflito usa stage_models.conflito para o dispatch de rework."""
        ctx = _make_ctx()
        result = self._make_rework_result()

        capturado: list[dict] = []

        def _fake_rework(
            ctx: object, repo: str, issue: dict, pr_number: int,
            iteration: int, cfg: dict, **kw: object
        ) -> None:
            capturado.append({"agent": cfg.get("agent")})

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(stage_models={"conflito": "opus"})),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._rework_has_active", return_value=False),
            mock.patch("deployment.deployment._dispatch_rework", side_effect=_fake_rework),
            mock.patch("subprocess.run") as mock_sub,
        ):
            mock_sub.return_value = mock.MagicMock(
                returncode=0, stdout='[{"number": 55}]', stderr=""
            )
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_conflito(ctx)

        assert len(capturado) == 1
        assert capturado[0]["agent"] == "opus"


# ---------------------------------------------------------------------------
# Isolamento entre estágios — cada cron só age no seu estágio
# ---------------------------------------------------------------------------

class TestEstagioIsolamento:
    """Garante que um cron de estágio X não aciona ações do estágio Y."""

    def test_run_dev_nao_aciona_merge(self) -> None:
        """run_dev com scan retornando issue approved review não executa merge."""
        ctx = _make_ctx()

        from flow.audit.state_comment import StateComment, render
        from flow.domain.gates import WorkItem
        from flow.domain.state import Modifier, State
        from flow.scan.scanner import ScanResult

        sc = StateComment(workflow="feature (v1)", current_node="review",
                          status="reviewed", repo="owner/repo")
        sc.set_reviewer_result(approved=True, comments=[], sha="abc123")
        state_body = render(sc)

        approved_result = ScanResult(
            item=WorkItem(
                key="https://github.com/owner/repo/issues/42",
                title="[owner/repo] Feature X",
                labels=frozenset(["crewflow:review", "crewflow:reviewed"]),
            ),
            current_state=State.REVIEW,
            modifiers=frozenset([Modifier.REVIEWED]),
            dispatch_candidate=False,
            spec_valid=None,
            changed=True,
            reason="reviewer aprovado",
        )

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[approved_result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._execute_auto_merges") as mock_merge,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_provider = mock.MagicMock()
            mock_provider.get_state_comment = mock.MagicMock(return_value=state_body)
            mock_provider.get_pr_for_issue = mock.MagicMock(
                return_value={"headRefOid": "abc123"})
            mock_pf.return_value = mock_provider
            run_dev(ctx)

        mock_merge.assert_not_called()
        mock_dispatch.assert_not_called()

    def test_run_merge_nao_aciona_dispatch_dev(self) -> None:
        """run_merge com scan retornando issue em todo não faz dispatch dev."""
        ctx = _make_ctx()
        todo_result = _make_scan_result("crewflow:todo")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[todo_result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._execute_auto_merges") as mock_merge,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_merge(ctx)

        mock_merge.assert_not_called()
        mock_dispatch.assert_not_called()

    def test_cada_cron_processa_scan_independente(self) -> None:
        """Cada cron roda o scan completo mas filtra independentemente."""
        ctx_dev = _make_ctx()
        ctx_rev = _make_ctx()

        todo_result = _make_scan_result("crewflow:todo")
        review_result = _make_scan_result("crewflow:review")

        # Ambos os resultados no scan
        all_results = [todo_result, review_result]

        dispatch_dev_calls: list = []
        dispatch_reviewer_calls: list = []

        def _fake_dispatch(*args: object, **kw: object) -> None:
            dispatch_dev_calls.append(True)

        def _fake_reviewer(ctx: object, repo: str, issue: dict, cfg: dict) -> None:
            dispatch_reviewer_calls.append(True)

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=all_results),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._reviewer_has_active", return_value=False),
            mock.patch("deployment.deployment._dispatch", side_effect=_fake_dispatch),
            mock.patch("deployment.deployment._dispatch_reviewer",
                       side_effect=_fake_reviewer),
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()

            run_dev(ctx_dev)

        assert len(dispatch_dev_calls) == 1
        assert len(dispatch_reviewer_calls) == 0

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=all_results),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._reviewer_has_active", return_value=False),
            mock.patch("deployment.deployment._dispatch", side_effect=_fake_dispatch),
            mock.patch("deployment.deployment._dispatch_reviewer",
                       side_effect=_fake_reviewer),
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()

            run_reviewer(ctx_rev)

        # Após run_reviewer: _dispatch não foi chamado (=1 de antes), _dispatch_reviewer=1
        assert len(dispatch_dev_calls) == 1   # não aumentou
        assert len(dispatch_reviewer_calls) == 1


# ---------------------------------------------------------------------------
# Dry-run nos entrypoints por estágio
# ---------------------------------------------------------------------------

class TestDryRunPorEstagio:
    def test_run_dev_dry_run_nao_despacha(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CREWFLOW_DRY_RUN=1 → run_dev não chama _dispatch."""
        monkeypatch.setenv("CREWFLOW_DRY_RUN", "1")
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:todo")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_not_called()
        ctx.notify.assert_not_called()

    def test_run_reviewer_dry_run_nao_despacha(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CREWFLOW_DRY_RUN=1 → run_reviewer não chama _dispatch_reviewer."""
        monkeypatch.setenv("CREWFLOW_DRY_RUN", "1")
        ctx = _make_ctx()
        result = _make_scan_result("crewflow:review")

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._dispatch_reviewer") as mock_rev,
        ):
            mock_cache.return_value.__enter__ = mock.MagicMock(
                return_value=sqlite3.connect(":memory:"))
            mock_cache.return_value.__exit__ = mock.MagicMock(return_value=False)
            mock_pf.return_value = mock.MagicMock()
            run_reviewer(ctx)

        mock_rev.assert_not_called()
