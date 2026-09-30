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
