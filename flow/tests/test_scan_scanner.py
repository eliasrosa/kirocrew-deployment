"""Testes do scanner principal — mock no provider, sem rede."""

from __future__ import annotations

import sqlite3
from unittest import mock

import pytest

from flow.domain.state import State
from flow.ports.issue_provider import ProviderError
from flow.scan.cache import _SCHEMA, compute_hash, set_hash
from flow.scan.scanner import ScanResult, SquadScanConfig, scan_candidates

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(_SCHEMA)
    db.commit()
    return db


@pytest.fixture
def config() -> SquadScanConfig:
    return SquadScanConfig(
        squad_id="test",
        issue_provider="github",
        projects=("owner/repo",),
        repos=frozenset({"api-gateway2", "api-subscription2"}),
    )


def _item(
    key: str = "VGAT-1",
    title: str = "[api-gateway2] Fix",
    labels: list[str] | None = None,
) -> dict:
    return {"key": key, "title": title, "labels": labels or [], "parent_key": None}


# ---------------------------------------------------------------------------
# scan_candidates
# ---------------------------------------------------------------------------

class TestScanCandidates:
    def test_retorna_candidato_em_todo(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        item = _item("VGAT-1", labels=["crewflow:todo", "crewflow:feature"])
        provider = mock.MagicMock()
        # list_by_state é chamado para cada estado — só "todo" retorna algo
        provider.list_by_state.side_effect = lambda project, s: [item] if s == "crewflow:todo" else []

        results = scan_candidates(config, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        assert len(candidates) == 1
        assert candidates[0].item.key == "VGAT-1"
        assert candidates[0].current_state is State.TODO

    def test_ignora_issue_sem_label_crewflow(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        item = _item("VGAT-1", labels=["bug", "phase-1"])
        provider = mock.MagicMock()
        provider.list_by_state.return_value = [item]

        results = scan_candidates(config, provider, conn)
        assert results == []

    def test_nao_despacha_com_blocked(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        item = _item("VGAT-1", labels=["crewflow:todo", "crewflow:blocked"])
        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:todo" else []

        results = scan_candidates(config, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        assert len(candidates) == 0
        # Mas a issue aparece no resultado com changed=True (primeiro ciclo)
        assert any(r.item.key == "VGAT-1" for r in results)
        assert all(not r.dispatch_candidate for r in results if r.item.key == "VGAT-1")

    def test_nao_despacha_com_running(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        item = _item("VGAT-1", labels=["crewflow:todo", "crewflow:running"])
        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:todo" else []

        results = scan_candidates(config, provider, conn)
        assert all(not r.dispatch_candidate for r in results)

    def test_skip_se_labels_nao_mudaram(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        labels = ["crewflow:dev"]
        item = _item("VGAT-1", labels=labels)
        # Simula cache já populado com o mesmo hash
        set_hash(conn, "VGAT-1", compute_hash(labels))

        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:dev" else []

        results = scan_candidates(config, provider, conn)
        # dev não é candidato E hash não mudou → skip
        assert results == []

    def test_inclui_se_labels_mudaram(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        labels_novos = ["crewflow:todo"]
        item = _item("VGAT-1", labels=labels_novos)
        # Cache tinha hash diferente (era dev antes)
        set_hash(conn, "VGAT-1", compute_hash(["crewflow:dev"]))

        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:todo" else []

        results = scan_candidates(config, provider, conn)
        candidates = [r for r in results if r.dispatch_candidate]

        assert len(candidates) == 1
        assert candidates[0].changed is True

    def test_continua_se_um_projeto_falha(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        """Falha parcial não interrompe o scan dos outros projetos."""
        config_dois = SquadScanConfig(
            squad_id="test",
            issue_provider="github",
            projects=("owner/repo-a", "owner/repo-b"),
            repos=frozenset({"api-gateway2"}),
        )
        item_b = _item("B-1", labels=["crewflow:todo"])

        def list_by_state(project: str, state: str) -> list[dict]:
            if project == "owner/repo-a":
                raise ProviderError("repo-a inacessível")
            return [item_b] if state == "crewflow:todo" else []

        provider = mock.MagicMock()
        provider.list_by_state.side_effect = list_by_state

        # Não deve lançar mesmo com repo-a falhando
        results = scan_candidates(config_dois, provider, conn)
        assert any(r.item.key == "B-1" for r in results)

    def test_valida_spec_zero_token(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        """Issue em spec com título sem repo válido é flagrada (spec_valid=False)."""
        item = _item("VGAT-1", title="Fix sem colchete de repo", labels=["crewflow:spec"])
        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:spec" else []

        results = scan_candidates(config, provider, conn)

        spec_results = [r for r in results if r.current_state is State.SPEC]
        assert len(spec_results) == 1
        assert spec_results[0].spec_valid is False

    def test_spec_valida_com_repo_correto(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        item = _item("VGAT-1", title="[api-gateway2] Fix", labels=["crewflow:spec"])
        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:spec" else []

        results = scan_candidates(config, provider, conn)

        spec_results = [r for r in results if r.current_state is State.SPEC]
        assert len(spec_results) == 1
        assert spec_results[0].spec_valid is True

    def test_scan_result_tem_campos_corretos(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        item = _item("VGAT-1", labels=["crewflow:todo"])
        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:todo" else []

        results = scan_candidates(config, provider, conn)
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, ScanResult)
        assert r.current_state is State.TODO
        assert r.modifiers == frozenset()
        assert r.dispatch_candidate is True
        assert r.changed is True  # primeiro ciclo
        assert r.reason != ""

    def test_dedup_por_key_entre_projetos(self, conn: sqlite3.Connection) -> None:
        """Mesma issue listada por dois projetos → um único ScanResult (issue #43)."""
        config_dois = SquadScanConfig(
            squad_id="test",
            issue_provider="github",
            projects=("owner/repo-a", "owner/repo-b"),
            repos=frozenset({"api-gateway2"}),
        )
        item = _item("VGAT-1", labels=["crewflow:todo", "crewflow:feature"])
        provider = mock.MagicMock()
        # AMBOS os projetos retornam a MESMA issue em crewflow:todo
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:todo" else []

        results = scan_candidates(config_dois, provider, conn)

        assert len([r for r in results if r.item.key == "VGAT-1"]) == 1

    def test_dedup_emite_log_debug(self, conn: sqlite3.Connection) -> None:
        """A issue deduplicada gera um log DEBUG identificando a key e o projeto."""
        config_dois = SquadScanConfig(
            squad_id="test",
            issue_provider="github",
            projects=("owner/repo-a", "owner/repo-b"),
            repos=frozenset({"api-gateway2"}),
        )
        item = _item("VGAT-1", labels=["crewflow:todo"])
        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item] if s == "crewflow:todo" else []

        with mock.patch("flow.scan.scanner.logger") as mock_logger:
            scan_candidates(config_dois, provider, conn)

        debug_calls = [c for c in mock_logger.debug.call_args_list if "deduplicada" in str(c)]
        assert len(debug_calls) == 1
        # A mensagem carrega a key e o projeto onde foi deduplicada
        assert "VGAT-1" in debug_calls[0].args
        assert "owner/repo-b" in debug_calls[0].args

    def test_issues_distintas_nao_sao_deduplicadas(self, config: SquadScanConfig, conn: sqlite3.Connection) -> None:
        """Dedup é por key: issues diferentes não colidem."""
        item_a = _item("VGAT-1", labels=["crewflow:todo"])
        item_b = _item("VGAT-2", labels=["crewflow:todo"])
        provider = mock.MagicMock()
        provider.list_by_state.side_effect = lambda p, s: [item_a, item_b] if s == "crewflow:todo" else []

        results = scan_candidates(config, provider, conn)
        keys = {r.item.key for r in results if r.dispatch_candidate}
        assert keys == {"VGAT-1", "VGAT-2"}
