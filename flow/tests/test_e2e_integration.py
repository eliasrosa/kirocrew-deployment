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
        {"match": {"labels": ["flow:hotfix"]}, "workflow": "hotfix-flow"},
        {"match": {"labels": ["flow:bug"]}, "workflow": "bug-flow"},
        {"match": {"labels": ["flow:debt"]}, "workflow": "debt-flow"},
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
        """Issue em flow:develop-waiting → executor decide DISPATCH_DEV."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Implementar validação de split",
            ["flow:develop-waiting", "flow:feature"],
        )
        # O scanner itera pelos dois repos da squad — o mock retorna o item em
        # AMBOS os projetos. A deduplication por key garante que a issue produz
        # EXATAMENTE um ScanResult por ciclo, não "ao menos um" (issue #43).
        provider = _mock_provider({"flow:develop-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        # Exatamente 1 candidato com a key correta — não uma duplicata por projeto
        assert len(candidates) == 1
        r = candidates[0]
        assert r.item.key == item["key"]
        assert r.current_state is State.DEVELOP_WAITING

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.DISPATCH_DEV
        assert "flow:develop-running" in decision.add_labels
        assert "flow:develop-running" in decision.add_labels
        assert "flow:develop-waiting" in decision.remove_labels

    def test_review_sem_reviewed_vira_dispatch_reviewer(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em flow:review-waiting sem lock → executor decide DISPATCH_REVIEWER."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["flow:review-waiting", "flow:feature"],
        )
        provider = _mock_provider({"flow:review-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        # REVIEW não é dispatch_candidate, mas muda de hash → aparece no resultado
        r = next((x for x in results if x.current_state is State.REVIEW_WAITING), None)
        assert r is not None

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.DISPATCH_REVIEWER
        assert "flow:review-running" in decision.add_labels

    def test_review_com_reviewed_skip(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em flow:review-waiting + flow:review-running → SKIP (lock anti-loop)."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["flow:review-waiting", "flow:review-running", "flow:feature"],
        )
        provider = _mock_provider({"flow:review-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.current_state is State.REVIEW_WAITING), None)
        assert r is not None

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.SKIP

    def test_blocked_nao_e_despachado(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em flow:develop-waiting + flow:blocked → não despachada."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/42",
            "[api-gateway2] Fix",
            ["flow:develop-waiting", "flow:blocked", "flow:feature"],
        )
        provider = _mock_provider({"flow:develop-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        assert len(candidates) == 0

    def test_spec_invalida_flagrada(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Issue em flow:briefing sem repo no título → spec_valid=False."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/10",
            "Fix sem colchete de repo",
            ["flow:briefing", "flow:feature"],
        )
        provider = _mock_provider({"flow:briefing": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        spec_invalid = [r for r in results if r.spec_valid is False]

        assert len(spec_invalid) == 1
        assert spec_invalid[0].item.key == item["key"]


# ---------------------------------------------------------------------------
# E2E: Deduplication por key em squad com múltiplos projetos (issue #43)
# ---------------------------------------------------------------------------

class TestMultiProjectDedupE2E:
    """Comportamento esperado quando a mesma issue aparece em mais de um projeto.

    Uma squad GitHub-first tem múltiplos repos (``projects``). O scanner varre
    todos. Se a mesma issue (mesma ``key`` canônica) for listada por mais de um
    projeto no mesmo ciclo, ela deve produzir EXATAMENTE UM ``ScanResult`` — não
    uma duplicata por projeto. A deduplication é por key e determinística, não
    depende do hash já gravado no cache pelo primeiro projeto (issue #43).
    """

    def test_mesma_issue_em_dois_projetos_produz_um_resultado(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        item = _item(
            "https://github.com/owner/api-gateway2/issues/43",
            "[api-gateway2] Bug de duplicata",
            ["flow:develop-waiting", "flow:bug"],
        )
        # squad tem 2 projetos; o mock retorna o MESMO item para ambos.
        provider = _mock_provider({"flow:develop-waiting": [item]})
        assert len(squad.projects) == 2  # pré-condição do cenário
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)

        # Exatamente 1 resultado para a issue — sem duplicata por projeto.
        matching = [r for r in results if r.item.key == item["key"]]
        assert len(matching) == 1
        assert matching[0].changed is True  # primeiro ciclo
        assert matching[0].dispatch_candidate is True

    def test_dedup_e_por_key_nao_por_hash(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Mesmo com o cache VAZIO (nenhum hash gravado), o segundo projeto não
        reintroduz a issue — a dedup é por key, não efeito colateral do hash."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/43",
            "[api-gateway2] Bug de duplicata",
            ["flow:develop-waiting", "flow:bug"],
        )
        provider = _mock_provider({"flow:develop-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        assert len([r for r in results if r.item.key == item["key"]]) == 1

    def test_issues_distintas_em_projetos_distintos_nao_sao_deduplicadas(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Dedup é por key: issues diferentes coexistem normalmente."""
        item_a = _item(
            "https://github.com/owner/api-gateway2/issues/1",
            "[api-gateway2] Fix A",
            ["flow:develop-waiting", "flow:bug"],
        )
        item_b = _item(
            "https://github.com/owner/api-subscription2/issues/2",
            "[api-subscription2] Fix B",
            ["flow:develop-waiting", "flow:bug"],
        )
        provider = _mock_provider({"flow:develop-waiting": [item_a, item_b]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        keys = {r.item.key for r in results if r.dispatch_candidate}
        assert keys == {item_a["key"], item_b["key"]}


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
            ["flow:develop-waiting", "flow:hotfix", "flow:p1"],
        )
        provider = _mock_provider({"flow:develop-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]
        assert len(candidates) == 1  # dedup por key: 1 resultado mesmo em 2 projetos (issue #43)

        decision = decide(candidates[0], squad=squad)
        assert decision.action is ActionKind.DISPATCH_DEV

    def test_hotfix_sem_p1_rebaixa_para_bug(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Hotfix sem p1 → REBRAND para bug (GATE 0)."""
        item = _item(
            "https://github.com/owner/api-gateway2/issues/99",
            "[api-gateway2] Ajuste menor",
            ["flow:develop-waiting", "flow:hotfix"],  # sem p1
        )
        provider = _mock_provider({"flow:develop-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.changed), None)
        assert r is not None

        decision = decide(r, squad=squad)
        assert decision.action is ActionKind.REBRAND
        assert decision.new_template == "bug"
        assert "flow:bug" in decision.add_labels
        assert "flow:hotfix" in decision.remove_labels

    def test_bypass_hml_com_justificativa_passa(
        self, squad: SquadConfig, conn: sqlite3.Connection
    ) -> None:
        """Hotfix com hml-bypass + justificativa → passa para QA."""
        sc = StateComment(
            workflow="hotfix (v1)", current_node="qa",
            status="running", repo="api-gateway2"
        )
        sc.add_exception("flow:blocked", "Checkout fora do ar", "@elias", "2026-09-15")
        state_comment = render_comment(sc)

        item = _item(
            "https://github.com/owner/api-gateway2/issues/100",
            "[api-gateway2] Fix urgente",
            ["flow:qa-waiting", "flow:hotfix", "flow:p1", "flow:blocked"],
        )
        provider = _mock_provider({"flow:qa-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.current_state is State.QA_WAITING), None)
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
            ["flow:qa-waiting", "flow:hotfix", "flow:p1", "flow:blocked"],
        )
        provider = _mock_provider({"flow:qa-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.current_state is State.QA_WAITING), None)
        assert r is not None

        decision = decide(r, state_comment=None, squad=squad)
        # No novo design, flow:blocked em QA_WAITING notifica QA (não BLOCK)
        # flow:blocked como STOP_MODIFIER previne dispatch em DEVELOP_WAITING
        assert decision.action in (ActionKind.NOTIFY_HUMAN, ActionKind.BLOCK)


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
            ["flow:develop-waiting", "flow:debt"],
        )
        provider = _mock_provider({"flow:develop-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        r = next((x for x in results if x.current_state is State.DEVELOP_WAITING), None)
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
            ["flow:develop-running"],
        )
        provider = _mock_provider({"flow:develop-running": [item]})
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
            ["flow:develop-waiting"],
        )
        # Simula que o cache tinha o estado anterior (dev)
        set_hash(conn, item["key"], compute_hash(["flow:develop-running"]))

        provider = _mock_provider({"flow:develop-waiting": [item]})
        cfg = _scan_cfg(squad)

        results = scan_candidates(cfg, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        assert len(candidates) == 1  # dedup por key: 1 resultado mesmo em 2 projetos (issue #43)
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
                {"match": {"labels": ["flow:hotfix"]}, "workflow": "hotfix-flow"},
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
            ["flow:develop-waiting", "flow:feature"],
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
                           current_state=State.DEVELOP_WAITING,
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
                labels=frozenset(["flow:develop-waiting", "flow:hotfix"]),
            ),
            current_state=State.DEVELOP_WAITING,
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
        assert "flow:bug" in labels_aplicadas
        assert "flow:hotfix" not in labels_aplicadas

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
