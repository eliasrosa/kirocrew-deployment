"""Testes de integração end-to-end do KiroCrew Flow.

Cobre o loop completo sem I/O real:
  squads/*.yaml → SquadConfig → scan_candidates() → executor.decide() → deployment.run()

O provider é mockado em memória. Nenhuma rede, nenhum Jira, nenhum GitHub.
O objetivo é garantir que as camadas se conectam corretamente e que o
comportamento end-to-end corresponde ao que está nos diagramas.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# Garante que deployment/ é importável (mesmo caminho que o cron usa)
_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from flow.audit.state_comment import StateComment  # noqa: E402
from flow.audit.state_comment import render as render_comment  # noqa: E402
from flow.config.squad import SquadConfig, _parse_squad  # noqa: E402
from flow.domain.state import State  # noqa: E402
from flow.executor.executor import ActionKind, decide  # noqa: E402
from flow.scan.cache import _SCHEMA, compute_hash  # noqa: E402
from flow.scan.scanner import ScanResult, SquadScanConfig, scan_candidates  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures e helpers
# ---------------------------------------------------------------------------

SQUAD_DICT: dict[str, Any] = {
    "id": "test-squad",
    "issue_provider": "github",
    "repos": ["owner/api-gateway2", "owner/api-subscription2"],
    "routing": [
        {"match": {"labels": ["crewflow:hotfix"]}, "workflow": "hotfix-flow"},
        {"match": {"labels": ["crewflow:bug"]}, "workflow": "bug-flow"},
        {"match": {"labels": ["crewflow:debt"]}, "workflow": "debt-flow"},
        {"default": "feature-flow"},
    ],
}


@pytest.fixture
def squad() -> SquadConfig:
    return _parse_squad(SQUAD_DICT)


@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(_SCHEMA)
    db.commit()
    return db


def _item(key: str, title: str, labels: list[str]) -> dict:
    return {"key": key, "title": title, "labels": labels, "parent_key": None}


def _mock_provider(items_by_state: dict[str, list[dict]]) -> mock.MagicMock:
    """Cria um provider mockado que retorna items específicos por estado."""
    provider = mock.MagicMock()
    provider.list_by_state.side_effect = lambda project, state: items_by_state.get(state, [])
    provider.get_state_comment.return_value = None
    provider.set_labels.return_value = None
    provider.upsert_state_comment.return_value = None
    return provider


def _scan_cfg(squad: SquadConfig) -> SquadScanConfig:
    return SquadScanConfig(
        squad_id=squad.id,
        issue_provider=squad.issue_provider,
        projects=tuple(squad.projects),
        repos=squad.repos,
    )


# ---------------------------------------------------------------------------
# E2E: Feature flow completo
# ---------------------------------------------------------------------------

class TestFeatureFlowE2E:
    def test_todo_vira_dispatch_dev(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em crewflow:todo → executor decide DISPATCH_DEV."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Implementar validação de split",
            ["crewflow:todo", "crewflow:feature"],
        )
        # O scanner itera pelos dois repos da squad — o mock retorna o item em ambos,
        # mas o deduplication por key garante que só aparece uma vez por estado.
        provider = _mock_provider({"crewflow:todo": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        # Deve haver ao menos 1 candidato com a key correta
        assert len(candidates) >= 1
        r = candidates[0]
        assert r.item.key == item["key"]
        assert r.current_state is State.TODO

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.DISPATCH_DEV
        assert "crewflow:dev" in decision.add_labels
        assert "crewflow:running" in decision.add_labels
        assert "crewflow:todo" in decision.remove_labels

    def test_review_sem_reviewed_vira_dispatch_reviewer(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em crewflow:review sem lock → executor decide DISPATCH_REVIEWER."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["crewflow:review", "crewflow:feature"],
        )
        provider = _mock_provider({"crewflow:review": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        # REVIEW não é dispatch_candidate, mas muda de hash → aparece no resultado
        r = next((x for x in results if x.current_state is State.REVIEW), None)
        assert r is not None

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.DISPATCH_REVIEWER
        assert "crewflow:reviewed" in decision.add_labels

    def test_review_com_reviewed_skip(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em crewflow:review + crewflow:reviewed → SKIP (lock anti-loop)."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["crewflow:review", "crewflow:reviewed", "crewflow:feature"],
        )
        provider = _mock_provider({"crewflow:review": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.current_state is State.REVIEW), None)
        assert r is not None

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.SKIP

    def test_blocked_nao_e_despachado(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em crewflow:todo + crewflow:blocked → não despachada."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["crewflow:todo", "crewflow:blocked", "crewflow:feature"],
        )
        provider = _mock_provider({"crewflow:todo": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        assert len(candidates) == 0

    def test_spec_invalida_flagrada(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em crewflow:spec sem repo no título → spec_valid=False."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/10",
            "Fix sem colchete de repo",
            ["crewflow:spec", "crewflow:feature"],
        )
        provider = _mock_provider({"crewflow:spec": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        spec_invalid = [r for r in results if r.spec_valid is False]

        assert len(spec_invalid) == 1
        assert spec_invalid[0].item.key == item["key"]


# ---------------------------------------------------------------------------
# E2E: Hotfix flow
# ---------------------------------------------------------------------------

class TestHotfixFlowE2E:
    def test_hotfix_com_p1_despacha(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Hotfix com p1 → DISPATCH_DEV (vai para produção)."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/99",
            "[api-gateway2] Fix urgente checkout",
            ["crewflow:todo", "crewflow:hotfix", "crewflow:p1"],
        )
        provider = _mock_provider({"crewflow:todo": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]
        assert len(candidates) >= 1

        decision = decide(candidates[0], squad=squad)
        assert decision.action is ActionKind.DISPATCH_DEV

    def test_hotfix_sem_p1_rebaixa_para_bug(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Hotfix sem p1 → REBRAND para bug (GATE 0)."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/99",
            "[api-gateway2] Ajuste menor",
            ["crewflow:todo", "crewflow:hotfix"],  # sem p1
        )
        provider = _mock_provider({"crewflow:todo": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.changed), None)
        assert r is not None

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.REBRAND
        assert decision.new_template == "bug"
        assert "crewflow:bug" in decision.add_labels
        assert "crewflow:hotfix" in decision.remove_labels

    def test_bypass_hml_com_justificativa_passa(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Hotfix com hml-bypass + justificativa → passa para QA."""
        sc = StateComment(
            workflow="hotfix (v1)", current_node="qa",
            status="running", repo="api-gateway2"
        )
        sc.add_exception("crewflow:hml-bypass", "Checkout fora do ar", "@elias", "2026-09-15")
        state_comment = render_comment(sc)

        item = _item(
            "https://github.com/owner/api-gateway2/issues/100",
            "[api-gateway2] Fix urgente",
            ["crewflow:qa", "crewflow:hotfix", "crewflow:p1", "crewflow:hml-bypass"],
        )
        provider = _mock_provider({"crewflow:qa": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.current_state is State.QA), None)
        assert r is not None

        decision = decide(r, state_comment=state_comment, squad=squad)
        assert decision.action is not ActionKind.BLOCK

    def test_bypass_hml_sem_justificativa_bloqueia(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Hotfix com hml-bypass sem justificativa → BLOCK."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/100",
            "[api-gateway2] Fix urgente",
            ["crewflow:qa", "crewflow:hotfix", "crewflow:p1", "crewflow:hml-bypass"],
        )
        provider = _mock_provider({"crewflow:qa": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.current_state is State.QA), None)
        assert r is not None

        decision = decide(r, state_comment=None, squad=squad)
        assert decision.action is ActionKind.BLOCK


# ---------------------------------------------------------------------------
# E2E: Debt flow
# ---------------------------------------------------------------------------

class TestDebtFlowE2E:
    def test_debt_todo_notifica_tl(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Débito técnico em todo → TL deve aprovar (GATE DT)."""
        from flow.executor.executor import HumanRole
        item = _item(
            "https://github.com/owner/api-gateway2/issues/50",
            "[api-gateway2] Refatorar módulo de cache",
            ["crewflow:todo", "crewflow:debt"],
        )
        provider = _mock_provider({"crewflow:todo": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.current_state is State.TODO), None)
        assert r is not None

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.NOTIFY_HUMAN
        assert decision.notify_role is HumanRole.TL


# ---------------------------------------------------------------------------
# E2E: Cache e idempotência
# ---------------------------------------------------------------------------

class TestCacheE2E:
    def test_segunda_passagem_sem_mudanca_retorna_vazio(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Scan idempotente: sem mudança de labels → sem resultado."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["crewflow:dev"],
        )
        provider = _mock_provider({"crewflow:dev": [item]})
        cfg = _scan_cfg(squad)

        # Primeira passagem — cria o cache
        results1 = scan_candidates(cfg, provider, conn)
        assert len(results1) > 0  # novo no cache

        # Segunda passagem — sem mudança
        results2 = scan_candidates(cfg, provider, conn)
        assert results2 == []  # nada mudou → silêncio total

    def test_mudanca_de_estado_detectada(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Scan detecta quando labels mudam entre ciclos."""
        from flow.scan.cache import set_hash

        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["crewflow:todo"],
        )
        # Simula que o cache tinha o estado anterior (dev)
        set_hash(conn, item["key"], compute_hash(["crewflow:dev"]))

        provider = _mock_provider({"crewflow:todo": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        assert len(candidates) >= 1
        assert candidates[0].changed is True


# ---------------------------------------------------------------------------
# E2E: deployment.run() com executor integrado
# ---------------------------------------------------------------------------

class TestDeploymentRunE2E:
    def _make_ctx(self) -> mock.MagicMock:
        ctx = mock.MagicMock()
        ctx._port = 5000
        ctx._secret = "secret"
        ctx.job.id = "test-job"
        return ctx

    def _config(self, **overrides: object) -> dict:
        base = {
            "repos": ["owner/api-gateway2"],
            "auto_dispatch": False,
            "max_concurrent": 2,
            "one_per_repo": True,
            "notify_chat_id": "",
            "squad_id": "test",
            "issue_provider": "github",
            "dev_root": "/tmp/dev",
            "agent": "kirocrew",
            "routing": [
                {"match": {"labels": ["crewflow:hotfix"]}, "workflow": "hotfix-flow"},
                {"default": "feature-flow"},
            ],
        }
        base.update(overrides)
        return base

    def test_feature_todo_avisa_em_fase1(self) -> None:
        """Fase 1 (auto=false): feature em todo → notifica."""
        from deployment.deployment import run

        ctx = self._make_ctx()
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["crewflow:todo", "crewflow:feature"],
        )

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=self._config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[ScanResult(
                           item=__import__("flow.domain.gates",
                               fromlist=["WorkItem"]).WorkItem(
                               key=item["key"], title=item["title"],
                               labels=frozenset(item["labels"])),
                           current_state=State.TODO,
                           modifiers=frozenset(),
                           dispatch_candidate=True,
                           spec_valid=None,
                           changed=True,
                           reason="test",
                       )]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            run(ctx)

        ctx.notify.assert_called()
        msg = ctx.notify.call_args[0][0]
        assert "Fase 1" in msg or "pronta" in msg.lower() or "ready" in msg.lower()

    def test_hotfix_sem_p1_rebrand_aplica_labels(self) -> None:
        """Hotfix sem p1 → executor decide REBRAND → deployment aplica labels."""
        from deployment.deployment import run

        ctx = self._make_ctx()

        from flow.domain.gates import WorkItem
        hotfix_result = ScanResult(
            item=WorkItem(
                key="https://github.com/owner/api-gateway2/issues/99",
                title="[api-gateway2] Ajuste menor",
                labels=frozenset(["crewflow:todo", "crewflow:hotfix"]),
            ),
            current_state=State.TODO,
            modifiers=frozenset(),
            dispatch_candidate=True,
            spec_valid=None,
            changed=True,
            reason="test",
        )

        provider_mock = mock.MagicMock()
        provider_mock.get_state_comment.return_value = None

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=self._config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[hotfix_result]),
            mock.patch("deployment.deployment.provider_for",
                       return_value=provider_mock),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            run(ctx)

        # set_labels deve ter sido chamado com as labels do REBRAND
        provider_mock.set_labels.assert_called_once()
        call_args = provider_mock.set_labels.call_args[0]
        labels_aplicadas = call_args[2]  # terceiro argumento: labels
        assert "crewflow:bug" in labels_aplicadas
        assert "crewflow:hotfix" not in labels_aplicadas

    def test_sem_resultados_silencio_total(self) -> None:
        """Sem nada a fazer: ctx.notify nunca chamado."""
        from deployment.deployment import run

        ctx = self._make_ctx()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=self._config()),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            run(ctx)

        ctx.notify.assert_not_called()
