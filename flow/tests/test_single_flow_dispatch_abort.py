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


class _FakeProvider:
    """Provider fake: get_work_item devolve o title, como o github_client real."""

    def __init__(self, item: dict | None = None, raises: bool = False) -> None:
        self._item = item if item is not None else {"key": "owner/repo#292", "title": "docs: single-flow", "labels": ["crewflow:todo"]}
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def get_work_item(self, project: str, key: str) -> dict:
        self.calls.append((project, key))
        if self._raises:
            raise RuntimeError("gh falhou")
        return dict(self._item)


def _dispatcher(provider: _FakeProvider | None = None) -> _LedgerDispatcher:
    return _LedgerDispatcher(ctx=mock.MagicMock(), cfg={}, provider=provider or _FakeProvider())


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
        # Sucesso agora devolve um marcador de sessão (str truthy), não True,
        # para o motor gravar como stage_session (Gap C). Continua sendo o
        # sinal de "despachou".
        result = _dispatcher().dispatch_stage(_REPO, _ISSUE, stage)
        assert result  # truthy
        assert isinstance(result, str)
        assert stage.value in result


def test_estagio_nao_ativo_retorna_false() -> None:
    # develop/review/qa não têm dispatcher no single-flow → sempre False.
    assert _dispatcher().dispatch_stage(_REPO, _ISSUE, State.DEVELOP_WAITING) is False


# ── Gap B: o dispatcher enriquece o issue com title via get_work_item ────────
#
# O motor (ledger_tick) monta o issue só com {number, key} — sem title. O
# dispatch montava o session_title com issue['title'] e estourava KeyError, o
# que criava o lock e abortava sem NUNCA subir a sessão sidebar. O fix busca o
# work_item e injeta o title antes de despachar.


@pytest.mark.parametrize("stage", [State.BRIEFING, State.PLANNING_SPECS])
def test_gapb_enriquece_issue_com_title_antes_do_dispatch(stage: State) -> None:
    prov = _FakeProvider({"key": "owner/repo#292", "title": "docs: single-flow", "labels": []})
    captured: dict = {}

    def _fake_dispatch(_ctx, _repo, issue, _cfg):
        captured.update(issue)
        return True

    target = "_dispatch_briefing" if stage is State.BRIEFING else "_dispatch_planning"
    with mock.patch(f"{_MOD}.{target}", side_effect=_fake_dispatch):
        # o issue do motor NÃO tem title
        assert _dispatcher(prov).dispatch_stage(_REPO, {"number": 292, "key": "owner/repo#292"}, stage)

    # o dispatch recebeu o issue JÁ com title (Gap B fechado)
    assert captured.get("title") == "docs: single-flow"
    # e number/key do motor foram preservados
    assert captured.get("number") == 292
    assert captured.get("key") == "owner/repo#292"
    # get_work_item foi chamado 1x com o repo e a key
    assert prov.calls == [(_REPO, "owner/repo#292")]


@pytest.mark.parametrize("stage", [State.BRIEFING, State.PLANNING_SPECS])
def test_gapb_get_work_item_falho_aborta_sem_avancar(stage: State) -> None:
    prov = _FakeProvider(raises=True)
    target = "_dispatch_briefing" if stage is State.BRIEFING else "_dispatch_planning"
    with mock.patch(f"{_MOD}.{target}") as m:
        # get_work_item lança -> enrich retorna None -> dispatch aborta (False)
        assert _dispatcher(prov).dispatch_stage(_REPO, _ISSUE, stage) is False
        # o _dispatch_* NÃO foi chamado (abortou antes)
        m.assert_not_called()
