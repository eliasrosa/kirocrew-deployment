"""Teste do Gap A: o dispatcher do single-flow NÃO reporta sucesso num abort.

O bug (achado ao rodar a primeira task real, #292): ``_LedgerDispatcher.
dispatch_stage`` devolvia ``True`` hardcoded e ``_dispatch_spec_stage``
devolvia ``None``, então um dispatch abortado (issue CLOSED, backstop lock já
existente, template inválido, POST falho) era lido como sucesso pelo ``tick``,
que avançava o ledger no vazio — a task andava de estágio sem nenhuma sessão
ter rodado.

O contrato correto, testado aqui:
  - abort  -> dispatch_stage retorna False (tick faz `dispatch-aborted`, NÃO avança)
  - POST OK -> dispatch_stage retorna True  (tick faz `dispatched`, avança)

Tudo mocado nos pontos de decisão do _dispatch_spec_stage; zero I/O de rede.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from deployment.deployment import _LedgerDispatcher  # noqa: E402
from flow.domain.state import State  # noqa: E402

_MOD = "deployment.deployment"
_ISSUE = {"number": 292, "key": "owner/repo#292"}
_REPO = "owner/repo"


def _dispatcher() -> _LedgerDispatcher:
    return _LedgerDispatcher(ctx=mock.MagicMock(), cfg={})


@pytest.mark.parametrize("stage", [State.BRIEFING, State.PLANNING_SPECS])
def test_abort_issue_closed_retorna_false(stage: State) -> None:
    with mock.patch(f"{_MOD}._is_issue_closed", return_value=True):
        assert _dispatcher().dispatch_stage(_REPO, _ISSUE, stage) is False


@pytest.mark.parametrize("stage", [State.BRIEFING, State.PLANNING_SPECS])
def test_abort_backstop_lock_existente_retorna_false(stage: State) -> None:
    with (
        mock.patch(f"{_MOD}._is_issue_closed", return_value=False),
        mock.patch(f"{_MOD}._try_acquire_dispatch_lock", return_value=(False, "/x.lock")),
    ):
        assert _dispatcher().dispatch_stage(_REPO, _ISSUE, stage) is False


@pytest.mark.parametrize("stage", [State.BRIEFING, State.PLANNING_SPECS])
def test_abort_post_falho_retorna_false(stage: State) -> None:
    with (
        mock.patch(f"{_MOD}._is_issue_closed", return_value=False),
        mock.patch(f"{_MOD}._try_acquire_dispatch_lock", return_value=(True, "/x.lock")),
        mock.patch(f"{_MOD}._spec_stage_prompt", return_value="prompt"),
        mock.patch(f"{_MOD}._post_agent_session", return_value=False),
    ):
        assert _dispatcher().dispatch_stage(_REPO, _ISSUE, stage) is False


@pytest.mark.parametrize("stage", [State.BRIEFING, State.PLANNING_SPECS])
def test_sucesso_post_ok_retorna_true(stage: State) -> None:
    with (
        mock.patch(f"{_MOD}._is_issue_closed", return_value=False),
        mock.patch(f"{_MOD}._try_acquire_dispatch_lock", return_value=(True, "/x.lock")),
        mock.patch(f"{_MOD}._spec_stage_prompt", return_value="prompt"),
        mock.patch(f"{_MOD}._post_agent_session", return_value=True),
    ):
        assert _dispatcher().dispatch_stage(_REPO, _ISSUE, stage) is True


def test_estagio_nao_ativo_retorna_false() -> None:
    # develop/review/qa não têm dispatcher no single-flow → sempre False.
    assert _dispatcher().dispatch_stage(_REPO, _ISSUE, State.DEVELOP_WAITING) is False
