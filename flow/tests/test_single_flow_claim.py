"""Testes da CAMADA DE ENTRADA do single-flow (frente 8).

A entrada é o que faltava para o motor rodar de verdade: sem ela,
``ledger.active()`` é sempre None e o tick fica idle eterno. Cobre:

  - `_parse_claim_message`: extração de owner/repo + número dos formatos aceitos
  - `_resolve_squad_id`: id do yaml (não 'default') — Gap 1
  - `claim_single_flow`: registra a task no ledger em flow:briefing — Gap 2
  - repo gravado como owner/repo (= project do get_work_item) — Gap 3
  - idempotência e limite de 1 task ativa por vez

Tudo mocado, zero I/O de rede. O SqliteRunLedger real roda em tmp_path via
monkeypatch, então nada toca ~/.kiro.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from deployment.deployment import (  # noqa: E402
    _parse_claim_message,
    _resolve_squad_id,
    claim_single_flow,
)
from flow.domain.run_ledger import SqliteRunLedger  # noqa: E402
from flow.domain.state import State  # noqa: E402

# ---------------------------------------------------------------------------
# _parse_claim_message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("owner/repo#42", ("owner/repo", 42)),
        ("owner/repo 99", ("owner/repo", 99)),
        ("owner/repo/issues/7", ("owner/repo", 7)),
        ("https://github.com/eliasrosa/kirocrew-flow/issues/288",
         ("eliasrosa/kirocrew-flow", 288)),
        ("  eliasrosa/kirocrew-flow#12  ", ("eliasrosa/kirocrew-flow", 12)),
        # Formatos Jira
        ("VGAT-1009", ("VGAT", "VGAT-1009")),
        ("VSUS-42", ("VSUS", "VSUS-42")),
        ("PROJ-1", ("PROJ", "PROJ-1")),
    ],
)
def test_parse_claim_message_formats(message: str, expected: tuple[str, int]) -> None:
    assert _parse_claim_message(message) == expected


@pytest.mark.parametrize("message", ["", "   ", "sem-numero", "42", "owner/repo"])
def test_parse_claim_message_rejects_invalid(message: str) -> None:
    with pytest.raises(ValueError):
        _parse_claim_message(message)


# ---------------------------------------------------------------------------
# _resolve_squad_id (Gap 1)
# ---------------------------------------------------------------------------

def test_resolve_squad_id_from_yaml(tmp_path: Path) -> None:
    """O id vem do yaml de squad_config, NÃO do 'default'."""
    yaml_file = tmp_path / "squad.yaml"
    yaml_file.write_text(
        "id: kirocrew-flow\n"
        "issue_provider: github\n"
        "repos:\n"
        "  - eliasrosa/kirocrew-flow\n",
        encoding="utf-8",
    )
    assert _resolve_squad_id({"squad_config": str(yaml_file)}) == "kirocrew-flow"


def test_resolve_squad_id_fallback_to_config_key() -> None:
    assert _resolve_squad_id({"squad_id": "minha-squad"}) == "minha-squad"


def test_resolve_squad_id_fallback_default() -> None:
    assert _resolve_squad_id({}) == "default"


def test_resolve_squad_id_missing_yaml_falls_back() -> None:
    """squad_config apontando para arquivo inexistente cai no squad_id/config."""
    assert _resolve_squad_id(
        {"squad_config": "/nao/existe.yaml", "squad_id": "fb"}
    ) == "fb"


# ---------------------------------------------------------------------------
# claim_single_flow (Gap 2 + Gap 3)
# ---------------------------------------------------------------------------

def _make_ctx(message: str) -> mock.MagicMock:
    ctx = mock.MagicMock()
    ctx.message = message
    return ctx


def _patch_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Faz o SqliteRunLedger do deployment abrir em tmp_path (não ~/.kiro)."""
    import deployment.deployment as dep

    def _factory(squad_id: str):  # type: ignore[no-untyped-def]
        return SqliteRunLedger(squad_id=squad_id, data_dir=tmp_path)

    monkeypatch.setattr(dep, "_load_config", lambda: {"squad_id": "test"})
    # SqliteRunLedger é importado localmente dentro da função; monkeypatch no
    # módulo de origem cobre o import interno.
    import flow.domain.run_ledger as rl
    monkeypatch.setattr(
        rl, "SqliteRunLedger",
        lambda squad_id: SqliteRunLedger(squad_id=squad_id, data_dir=tmp_path),
    )


def test_claim_registers_task_in_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_ledger(monkeypatch, tmp_path)
    ctx = _make_ctx("eliasrosa/kirocrew-flow#288")

    result = claim_single_flow(ctx)

    assert result == "claimed:eliasrosa/kirocrew-flow#288"
    # A task ficou ativa no ledger, em flow:briefing, com repo canônico.
    lg = SqliteRunLedger(squad_id="test", data_dir=tmp_path)
    try:
        run = lg.active()
        assert run is not None
        assert run.task_key == "eliasrosa/kirocrew-flow#288"
        assert run.repo == "eliasrosa/kirocrew-flow"  # Gap 3: project canônico
        assert run.current_stage == State.BRIEFING.value
    finally:
        lg.close()
    ctx.notify.assert_called_once()


def test_claim_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_ledger(monkeypatch, tmp_path)
    ctx = _make_ctx("owner/repo#1")

    first = claim_single_flow(ctx)
    second = claim_single_flow(_make_ctx("owner/repo#1"))

    assert first == "claimed:owner/repo#1"
    assert second == "claimed:owner/repo#1"  # reclamar a mesma task = ok


def test_claim_rejects_second_active_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Limite de 1 task ativa: uma segunda task diferente é recusada."""
    _patch_ledger(monkeypatch, tmp_path)

    first = claim_single_flow(_make_ctx("owner/repo#1"))
    second = claim_single_flow(_make_ctx("owner/repo#2"))

    assert first == "claimed:owner/repo#1"
    assert second == "rejected:owner/repo#2"
    # A ativa continua sendo a #1.
    lg = SqliteRunLedger(squad_id="test", data_dir=tmp_path)
    try:
        run = lg.active()
        assert run is not None
        assert run.task_key == "owner/repo#1"
    finally:
        lg.close()


def test_claim_bad_message_returns_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_ledger(monkeypatch, tmp_path)
    ctx = _make_ctx("mensagem-invalida")

    result = claim_single_flow(ctx)

    assert result == "error:bad-message"
    ctx.notify.assert_called_once()


def test_claim_jira_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Claim de task Jira (VGAT-1009): task_key = chave Jira, repo = projeto."""
    _patch_ledger(monkeypatch, tmp_path)
    ctx = _make_ctx("VGAT-1009")

    result = claim_single_flow(ctx)

    assert result == "claimed:VGAT-1009"
    lg = SqliteRunLedger(squad_id="test", data_dir=tmp_path)
    try:
        run = lg.active()
        assert run is not None
        assert run.task_key == "VGAT-1009"
        assert run.repo == "VGAT"           # projeto Jira como "repo"
        assert run.current_stage == State.BRIEFING.value
    finally:
        lg.close()
