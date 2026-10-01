"""C3 da frente estado-local (#301): _LedgerStateReader.read_state deriva o
estado da esteira de EVIDÊNCIA externa (branch/PR/issue fechada), NÃO de labels.

Antes da C3, read_state fazia ``parse_state(item["labels"])`` — o single-flow
dependia das labels ``flow:*`` no GitHub. Agora deriva de ``implicit_state``
(branch existe? PR? PR aprovada? issue fechada?), que é fato do repositório,
não label de estado. Assim o motor single-flow não lê mais nenhuma label.

Mocamos ``_collect_implicit_state`` (o ponto de I/O) para provar o mapeamento
ImplicitState → State sem tocar rede.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from deployment.deployment import _LedgerStateReader  # noqa: E402
from flow.domain.state import State  # noqa: E402
from flow.scan.scanner import ImplicitState  # noqa: E402

_MOD = "deployment.deployment"
_REPO = "owner/repo"
_KEY = "owner/repo#42"


@pytest.mark.parametrize(
    ("implicit", "expected"),
    [
        (ImplicitState.TODO,      State.DEVELOP_WAITING),
        (ImplicitState.DEV,       State.DEVELOP_RUNNING),
        (ImplicitState.REVIEW,    State.REVIEW_WAITING),
        (ImplicitState.REVIEW_OK, State.REVIEW_APPROVED),
        (ImplicitState.DONE,      State.DONE),
    ],
)
def test_read_state_mapeia_evidencia_para_state(
    implicit: ImplicitState, expected: State
) -> None:
    provider = mock.MagicMock()
    reader = _LedgerStateReader(provider)
    with mock.patch(f"{_MOD}._collect_implicit_state", return_value=implicit):
        result = reader.read_state(_REPO, _KEY)
    assert result is expected


def test_read_state_evidencia_indeterminada_retorna_none() -> None:
    # Sem evidência coletável (erro de I/O, issue sem branch/PR) → None.
    # O motor então AGUARDA (não avança), disciplina fail-safe.
    provider = mock.MagicMock()
    reader = _LedgerStateReader(provider)
    with mock.patch(f"{_MOD}._collect_implicit_state", return_value=None):
        result = reader.read_state(_REPO, _KEY)
    assert result is None


def test_read_state_nao_le_labels_do_provider() -> None:
    # Prova a essência da C3: mesmo que o provider devolva labels flow:* de
    # estado, read_state NÃO as usa — a fonte é a evidência (implicit_state).
    provider = mock.MagicMock()
    # get_work_item devolveria labels de review; mas o estado real (evidência)
    # diz DEVELOP_WAITING (sem branch/PR). O resultado deve seguir a EVIDÊNCIA.
    provider.get_work_item.return_value = {
        "key": _KEY, "title": "x", "labels": ["flow:review-waiting"],
    }
    reader = _LedgerStateReader(provider)
    with mock.patch(f"{_MOD}._collect_implicit_state", return_value=ImplicitState.TODO):
        result = reader.read_state(_REPO, _KEY)
    # Seguiu a evidência (TODO→DEVELOP_WAITING), NÃO a label (review-waiting).
    assert result is State.DEVELOP_WAITING


# ---------------------------------------------------------------------------
# Gap C: _single_flow_tick calcula stage_running via _issue_has_active_session
# ---------------------------------------------------------------------------

def test_single_flow_tick_passa_stage_running_quando_sessao_viva() -> None:
    """Gap C: com stage_session preenchido e sessão viva, o tick recebe
    stage_running=True e NÃO redispara (evita o enxame de sessões)."""
    from flow.domain.run_ledger import Run, RunStatus

    run_ativo = Run(
        task_key="owner/repo#42", repo="owner/repo",
        current_stage="flow:briefing", stage_session="flow:briefing:repo-42",
        started_at="", last_transition="", attempts=0, status=RunStatus.RUNNING,
    )
    fake_ledger = mock.MagicMock()
    fake_ledger.active.return_value = run_ativo

    captured: dict = {}

    def _fake_tick(ledger, dispatcher, reader, *, stage_running=False):
        captured["stage_running"] = stage_running
        return f"waiting:{run_ativo.current_stage}"

    with (
        mock.patch(f"{_MOD}._check_installed_version"),
        mock.patch(f"{_MOD}._load_config", return_value={"issue_provider": "github"}),
        mock.patch(f"{_MOD}._resolve_squad_id", return_value="kirocrew-flow"),
        mock.patch(f"{_MOD}.provider_for", return_value=mock.MagicMock()),
        mock.patch("flow.domain.run_ledger.SqliteRunLedger", return_value=fake_ledger),
        # Item A: spec-stage usa _spec_slot_is_running, NÃO _issue_has_active_session
        mock.patch(f"{_MOD}._spec_slot_is_running", return_value=True) as m_slot,
        mock.patch(f"{_MOD}._issue_has_active_session", return_value=False) as m_active,
        mock.patch("flow.engine.ledger_tick.tick", side_effect=_fake_tick),
    ):
        from deployment.deployment import _single_flow_tick
        result = _single_flow_tick(mock.MagicMock())

    assert captured["stage_running"] is True   # sessão viva → não redispara
    assert result.startswith("waiting:")
    m_slot.assert_called_once()        # spec-slot verificado
    m_active.assert_not_called()       # _issue_has_active_session NÃO chamado para spec-stage


def test_single_flow_tick_stage_running_false_quando_sem_stage_session() -> None:
    """Sem stage_session no ledger, não há sessão a 'prender' → stage_running=False."""
    from flow.domain.run_ledger import Run, RunStatus

    run_sem_sessao = Run(
        task_key="owner/repo#42", repo="owner/repo",
        current_stage="flow:briefing", stage_session=None,
        started_at="", last_transition="", attempts=0, status=RunStatus.RUNNING,
    )
    fake_ledger = mock.MagicMock()
    fake_ledger.active.return_value = run_sem_sessao

    captured: dict = {}

    def _fake_tick(ledger, dispatcher, reader, *, stage_running=False):
        captured["stage_running"] = stage_running
        return f"dispatched:{run_sem_sessao.current_stage}"

    with (
        mock.patch(f"{_MOD}._check_installed_version"),
        mock.patch(f"{_MOD}._load_config", return_value={"issue_provider": "github"}),
        mock.patch(f"{_MOD}._resolve_squad_id", return_value="kirocrew-flow"),
        mock.patch(f"{_MOD}.provider_for", return_value=mock.MagicMock()),
        mock.patch("flow.domain.run_ledger.SqliteRunLedger", return_value=fake_ledger),
        mock.patch(f"{_MOD}._spec_slot_is_running", return_value=True) as m_slot,
        mock.patch(f"{_MOD}._issue_has_active_session", return_value=True) as m_active,
        mock.patch("flow.engine.ledger_tick.tick", side_effect=_fake_tick),
    ):
        from deployment.deployment import _single_flow_tick
        _single_flow_tick(mock.MagicMock())

    assert captured["stage_running"] is False  # sem stage_session, nem checa
    m_slot.assert_not_called()
    m_active.assert_not_called()

# ---------------------------------------------------------------------------
# Item A — _spec_slot_is_running e bifurcação spec vs develop
# ---------------------------------------------------------------------------

class TestSpecSlotIsRunning:
    """_spec_slot_is_running consulta GET /api/chat/slots e retorna running."""

    def _make_response(self, slots: list) -> mock.MagicMock:
        import json as _json
        m = mock.MagicMock()
        m.read.return_value = _json.dumps(slots).encode()
        m.__enter__ = lambda s: s
        m.__exit__ = mock.MagicMock(return_value=False)
        return m

    def test_retorna_true_quando_slot_running(self) -> None:
        resp = self._make_response([
            {"key": "briefing-repo-42", "running": True},
            {"key": "outro-slot", "running": False},
        ])
        with mock.patch("urllib.request.urlopen", return_value=resp):
            from deployment.deployment import _spec_slot_is_running
            assert _spec_slot_is_running("briefing-repo-42", 5476, "secret") is True

    def test_retorna_false_quando_slot_nao_running(self) -> None:
        resp = self._make_response([
            {"key": "briefing-repo-42", "running": False},
        ])
        with mock.patch("urllib.request.urlopen", return_value=resp):
            from deployment.deployment import _spec_slot_is_running
            assert _spec_slot_is_running("briefing-repo-42", 5476, "secret") is False

    def test_retorna_false_quando_slot_nao_encontrado(self) -> None:
        resp = self._make_response([
            {"key": "outro-slot", "running": True},
        ])
        with mock.patch("urllib.request.urlopen", return_value=resp):
            from deployment.deployment import _spec_slot_is_running
            assert _spec_slot_is_running("briefing-repo-42", 5476, "secret") is False

    def test_fail_safe_retorna_false_em_erro_de_rede(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            from deployment.deployment import _spec_slot_is_running
            assert _spec_slot_is_running("briefing-repo-42", 5476, "secret") is False


def test_single_flow_tick_develop_usa_issue_has_active_session() -> None:
    """Develop-waiting usa _issue_has_active_session, NÃO _spec_slot_is_running."""
    from flow.domain.run_ledger import Run, RunStatus

    run_dev = Run(
        task_key="owner/repo#42", repo="owner/repo",
        current_stage="flow:develop-waiting",
        stage_session="algum-marcador-de-sessao",
        started_at="", last_transition="", attempts=0, status=RunStatus.RUNNING,
    )
    fake_ledger = mock.MagicMock()
    fake_ledger.active.return_value = run_dev

    captured: dict = {}

    def _fake_tick(ledger, dispatcher, reader, *, stage_running=False):
        captured["stage_running"] = stage_running
        return f"waiting:{run_dev.current_stage}"

    with (
        mock.patch(f"{_MOD}._check_installed_version"),
        mock.patch(f"{_MOD}._load_config", return_value={"issue_provider": "github"}),
        mock.patch(f"{_MOD}._resolve_squad_id", return_value="kirocrew-flow"),
        mock.patch(f"{_MOD}.provider_for", return_value=mock.MagicMock()),
        mock.patch("flow.domain.run_ledger.SqliteRunLedger", return_value=fake_ledger),
        mock.patch(f"{_MOD}._spec_slot_is_running", return_value=False) as m_slot,
        mock.patch(f"{_MOD}._issue_has_active_session", return_value=True) as m_active,
        mock.patch("flow.engine.ledger_tick.tick", side_effect=_fake_tick),
    ):
        from deployment.deployment import _single_flow_tick
        _single_flow_tick(mock.MagicMock())

    assert captured["stage_running"] is True   # _issue_has_active_session retornou True
    m_active.assert_called_once()              # foi chamado para develop-stage
    m_slot.assert_not_called()                 # _spec_slot_is_running NÃO chamado


# ---------------------------------------------------------------------------
# issue #311 — provider POR REPO em _LedgerStateReader / _LedgerDispatcher
# ---------------------------------------------------------------------------

def _squad_root_jira_override_github(repo_id: str):
    """SquadConfig com issue_provider raiz=jira e override github para repo_id."""
    from flow.config.squad import RepoConfig, SquadConfig

    return SquadConfig(
        id="voomp-squad-gw",
        name="Voomp Squad",
        issue_provider="jira",
        projects=[repo_id, "kdop/proj/gw2"],
        repos=frozenset([repo_id, "kdop/proj/gw2"]),
        workflow_template="versao-c",
        repo_configs=[RepoConfig(name=repo_id, issue_provider="github")],
    )


def test_read_state_resolve_provider_por_repo() -> None:
    """read_state usa squad.issue_provider_for(repo): github p/ repo override,
    jira p/ repo sem override. Provamos observando qual provider é passado a
    _collect_implicit_state via provider_for."""
    squad = _squad_root_jira_override_github(_REPO)
    default_provider = mock.MagicMock(name="default")
    gh_provider = mock.MagicMock(name="github")
    jira_provider = mock.MagicMock(name="jira")

    def _provider_for(name: str):
        return gh_provider if name == "github" else jira_provider

    reader = _LedgerStateReader(default_provider, squad=squad)

    with (
        mock.patch(f"{_MOD}.provider_for", side_effect=_provider_for),
        mock.patch(
            f"{_MOD}._collect_implicit_state", return_value=ImplicitState.TODO
        ) as m_collect,
    ):
        # repo COM override → provider github
        reader.read_state(_REPO, _KEY)
        assert m_collect.call_args.args[1] is gh_provider
        # repo SEM override → herda jira do raiz
        reader.read_state("kdop/proj/gw2", "kdop/proj/gw2#7")
        assert m_collect.call_args.args[1] is jira_provider


def test_read_state_sem_squad_usa_provider_default() -> None:
    """Sem squad, read_state usa o provider default (comportamento anterior)."""
    default_provider = mock.MagicMock(name="default")
    reader = _LedgerStateReader(default_provider)  # squad=None
    with mock.patch(
        f"{_MOD}._collect_implicit_state", return_value=ImplicitState.TODO
    ) as m_collect:
        reader.read_state(_REPO, _KEY)
    assert m_collect.call_args.args[1] is default_provider


def test_dispatcher_enrich_resolve_provider_por_repo() -> None:
    """_LedgerDispatcher._enrich_issue usa o provider por repo (github override)."""
    from deployment.deployment import _LedgerDispatcher

    squad = _squad_root_jira_override_github(_REPO)
    default_provider = mock.MagicMock(name="default")
    gh_provider = mock.MagicMock(name="github")
    gh_provider.get_work_item.return_value = {"title": "T", "number": 42}
    jira_provider = mock.MagicMock(name="jira")

    def _provider_for(name: str):
        return gh_provider if name == "github" else jira_provider

    dispatcher = _LedgerDispatcher(
        mock.MagicMock(), {"issue_provider": "jira"}, default_provider, squad=squad
    )
    with mock.patch(f"{_MOD}.provider_for", side_effect=_provider_for):
        enriched = dispatcher._enrich_issue(_REPO, {"number": 42, "key": _KEY})
    # provider github (override do repo) foi usado para buscar o work_item
    gh_provider.get_work_item.assert_called_once()
    jira_provider.get_work_item.assert_not_called()
    assert enriched is not None and enriched["title"] == "T"
