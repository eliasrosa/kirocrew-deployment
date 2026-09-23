"""Testes do monitor zero-token de issue (issue #211).

Cobre:
  - watch_issue.check: PR aberta (notifica 1x), issue fechada (Done),
    sessão morta (Done), ainda implementando (Skip silencioso), anti-spam.
  - _create_issue_monitor: registra o cron, é idempotente e fail-safe.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from deployment.deployment import _create_issue_monitor  # noqa: E402
from deployment.flow import watch_issue  # noqa: E402
from deployment.flow.watch_issue import Done, Skip, _parse_target, check  # noqa: E402

_TARGET = "owner/repo#42"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(message: str = _TARGET) -> mock.MagicMock:
    ctx = mock.MagicMock()
    ctx.message = message
    return ctx


def _patch_gh(monkeypatch: pytest.MonkeyPatch, issue: dict, prs: list) -> None:
    """Faz watch_issue._gh_json devolver issue/PRs conforme os args."""

    def fake_gh_json(args: list[str]) -> object:
        if args[0] == "issue":
            return issue
        if args[0] == "pr":
            return prs
        raise AssertionError(f"chamada gh inesperada: {args}")

    monkeypatch.setattr(watch_issue, "_gh_json", fake_gh_json)


@pytest.fixture(autouse=True)
def _isolate_markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isola o diretório de marcadores anti-spam em tmp."""
    monkeypatch.setattr(watch_issue, "_MARKER_DIR", str(tmp_path / "markers"))


# ---------------------------------------------------------------------------
# _parse_target
# ---------------------------------------------------------------------------

def test_parse_target_ok() -> None:
    assert _parse_target("eliasrosa/kirocrew-flow#211") == (
        "eliasrosa/kirocrew-flow",
        "211",
    )


@pytest.mark.parametrize("bad", ["", "owner/repo", "owner/repo#", "owner/repo#abc", "#42"])
def test_parse_target_invalid(bad: str) -> None:
    with pytest.raises(ValueError):
        _parse_target(bad)


# ---------------------------------------------------------------------------
# check() — caminhos principais
# ---------------------------------------------------------------------------

def test_check_issue_closed_notifies_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gh(
        monkeypatch,
        issue={"state": "CLOSED", "labels": []},
        prs=[{"number": 99, "state": "MERGED", "url": "u"}],
    )
    ctx = _ctx()
    with pytest.raises(Done):
        check(ctx)
    ctx.notify.assert_called_once()
    assert "fechou" in ctx.notify.call_args[0][0]
    assert "#99" in ctx.notify.call_args[0][0]


def test_check_pr_open_notifies_once_then_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gh(
        monkeypatch,
        issue={"state": "OPEN", "labels": []},
        prs=[{"number": 55, "state": "OPEN", "url": "http://pr/55"}],
    )
    ctx = _ctx()

    # 1º tick: notifica e segue monitorando (Skip)
    with pytest.raises(Skip):
        check(ctx)
    ctx.notify.assert_called_once()
    assert "#55" in ctx.notify.call_args[0][0]

    # 2º tick: PR ainda aberta → NÃO notifica de novo (anti-spam), segue Skip
    ctx.notify.reset_mock()
    with pytest.raises(Skip):
        check(ctx)
    ctx.notify.assert_not_called()


def test_check_no_pr_still_implementing_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gh(monkeypatch, issue={"state": "OPEN", "labels": []}, prs=[])
    # Sessão recém-criada (idle pequeno) → silêncio.
    monkeypatch.setattr(watch_issue, "_session_idle_secs", lambda r, n: 10.0)
    ctx = _ctx()
    with pytest.raises(Skip):
        check(ctx)
    ctx.notify.assert_not_called()


def test_check_no_pr_no_session_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gh(monkeypatch, issue={"state": "OPEN", "labels": []}, prs=[])
    # Sem arquivo de sessão → None → não conclui travamento, só silêncio.
    monkeypatch.setattr(watch_issue, "_session_idle_secs", lambda r, n: None)
    ctx = _ctx()
    with pytest.raises(Skip):
        check(ctx)
    ctx.notify.assert_not_called()


def test_check_dead_session_notifies_and_done(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gh(monkeypatch, issue={"state": "OPEN", "labels": []}, prs=[])
    # Sessão sem atividade acima do limite → travou.
    monkeypatch.setattr(
        watch_issue, "_session_idle_secs", lambda r, n: watch_issue._DEAD_SESSION_SECS + 60
    )
    ctx = _ctx()
    with pytest.raises(Done):
        check(ctx)
    ctx.notify.assert_called_once()
    assert "travad" in ctx.notify.call_args[0][0]


def test_check_pr_open_after_closed_wins_close(monkeypatch: pytest.MonkeyPatch) -> None:
    # Issue CLOSED tem prioridade mesmo com PR aberta listada (edge de corrida).
    _patch_gh(
        monkeypatch,
        issue={"state": "CLOSED", "labels": []},
        prs=[{"number": 7, "state": "OPEN", "url": "u"}],
    )
    ctx = _ctx()
    with pytest.raises(Done):
        check(ctx)


# ---------------------------------------------------------------------------
# _create_issue_monitor
# ---------------------------------------------------------------------------

def _install_fake_kiro_crew(
    monkeypatch: pytest.MonkeyPatch, cron_service: object
) -> None:
    """Injeta módulos kiro_crew falsos em sys.modules.

    O ``_create_issue_monitor`` importa ``kiro_crew.config.paths`` e
    ``kiro_crew.cron`` LAZILY dentro da função. Em CI o pacote real não está
    instalado, então injetamos stubs para exercitar a lógica sem depender dele.
    """
    import types

    pkg = types.ModuleType("kiro_crew")
    pkg.__path__ = []  # marca como pacote
    config_pkg = types.ModuleType("kiro_crew.config")
    config_pkg.__path__ = []
    paths_mod = types.ModuleType("kiro_crew.config.paths")
    paths_mod.config_dir = lambda: "/tmp/cfg"  # type: ignore[attr-defined]
    cron_mod = types.ModuleType("kiro_crew.cron")
    cron_mod.CronService = cron_service  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "kiro_crew", pkg)
    monkeypatch.setitem(sys.modules, "kiro_crew.config", config_pkg)
    monkeypatch.setitem(sys.modules, "kiro_crew.config.paths", paths_mod)
    monkeypatch.setitem(sys.modules, "kiro_crew.cron", cron_mod)


def test_create_issue_monitor_registers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_svc = mock.MagicMock()
    fake_svc.add_job_if_absent.return_value = mock.MagicMock()  # criou
    _install_fake_kiro_crew(monkeypatch, lambda base_dir: fake_svc)

    _create_issue_monitor("owner/repo", 42)

    fake_svc.add_job_if_absent.assert_called_once()
    kwargs = fake_svc.add_job_if_absent.call_args.kwargs
    assert kwargs["name"] == "watch-repo-42"
    assert kwargs["message"] == "owner/repo#42"
    assert kwargs["every_secs"] == 180
    assert kwargs["script"].endswith("watch_issue.py:check")
    assert kwargs["minimal_context"] is True
    assert kwargs["hide_in_chat"] is True
    assert kwargs["persistent_session"] is False


def test_create_issue_monitor_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    # add_job_if_absent devolve None quando o job já existe — não deve levantar.
    fake_svc = mock.MagicMock()
    fake_svc.add_job_if_absent.return_value = None
    _install_fake_kiro_crew(monkeypatch, lambda base_dir: fake_svc)

    _create_issue_monitor("owner/repo", 42)  # não levanta
    fake_svc.add_job_if_absent.assert_called_once()


def test_create_issue_monitor_failsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    # Qualquer erro ao criar o monitor NÃO pode propagar (dispatch preservado).
    def _boom(base_dir: str) -> object:
        raise RuntimeError("store indisponível")

    _install_fake_kiro_crew(monkeypatch, _boom)

    # Não deve levantar.
    _create_issue_monitor("owner/repo", 42)
