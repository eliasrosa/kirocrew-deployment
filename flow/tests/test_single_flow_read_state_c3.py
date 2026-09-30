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
        mock.patch(f"{_MOD}._issue_has_active_session", return_value=True) as m_active,
        mock.patch("flow.engine.ledger_tick.tick", side_effect=_fake_tick),
    ):
        from deployment.deployment import _single_flow_tick
        result = _single_flow_tick(mock.MagicMock())

    assert captured["stage_running"] is True   # sessão viva → não redispara
    assert result.startswith("waiting:")
    m_active.assert_called_once()


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
        mock.patch(f"{_MOD}._issue_has_active_session", return_value=True) as m_active,
        mock.patch("flow.engine.ledger_tick.tick", side_effect=_fake_tick),
    ):
        from deployment.deployment import _single_flow_tick
        _single_flow_tick(mock.MagicMock())

    assert captured["stage_running"] is False  # sem stage_session, nem checa
    m_active.assert_not_called()
