"""Testes para concorrência orientada ao estado da issue (issue #90).

Critérios de aceite:
  - Dispatch decidido pelo estado da issue, não por lock de tempo
  - Cada etapa marca o estado ao terminar (testado via funções de rastreamento)
  - Detecção de sessão morta sem travar a fila
  - Fail-closed em ambiguidade
  - Anti-duplo-dispatch pelo mecanismo de estado
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from unittest import mock

_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from deployment.deployment import (  # noqa: E402
    _DISPATCH_BACKSTOP_SECS,
    DEAD_SESSION_TIMEOUT_SECS,
    _is_dead_session,
    _issue_has_active_session,
    _lock_is_stale,
    _recover_dead_session,
    _repo_has_active,
    run_dev,
)
from flow.scan.cache import (  # noqa: E402
    clear_running_since,
    get_running_since,
    open_cache,
    set_running_since,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx() -> mock.MagicMock:
    ctx = mock.MagicMock()
    ctx._port = 5000
    ctx._secret = "secret"
    ctx.job.id = "test-job"
    return ctx


def _base_config(**overrides: object) -> dict:
    cfg: dict = {
        "repos": ["owner/repo"],
        "auto_dispatch": True,
        "max_concurrent_tasks": 2,
        "one_per_repo": False,
        "notify_chat_id": "",
        "squad_id": "test",
        "issue_provider": "github",
        "dev_root": "/tmp/dev",
        "agent": "kirocrew",
    }
    cfg.update(overrides)
    return cfg


def _make_scan_result(
    state_label: str,
    modifiers: list[str] | None = None,
    issue_number: int = 42,
) -> object:
    """Cria um ScanResult mínimo para o estado e modificadores dados."""
    from flow.domain.gates import WorkItem
    from flow.domain.state import State, parse_modifiers, parse_state
    from flow.scan.scanner import ScanResult

    labels_set = frozenset([state_label] + (modifiers or []))
    state = parse_state(labels_set)
    mods = parse_modifiers(labels_set)

    return ScanResult(
        item=WorkItem(
            key=f"https://github.com/owner/repo/issues/{issue_number}",
            title="[owner/repo] Test issue",
            labels=labels_set,
        ),
        current_state=state,
        modifiers=mods,
        dispatch_candidate=(state is State.DEVELOP_WAITING and not any(
            m in labels_set for m in ("flow:blocked", "flow:develop-running")
        )),
        spec_valid=None,
        changed=True,
        reason="test",
    )


# ---------------------------------------------------------------------------
# _lock_is_stale — backstop curto (2 minutos, não 2 horas)
# ---------------------------------------------------------------------------

class TestLockIsStaleBackstop:
    """Verifica que o backstop expirou após _DISPATCH_BACKSTOP_SECS, não 2h."""

    def test_constante_backstop_é_curta(self) -> None:
        """_DISPATCH_BACKSTOP_SECS deve ser no máximo 5 minutos."""
        assert _DISPATCH_BACKSTOP_SECS <= 300, (
            f"backstop muito longo: {_DISPATCH_BACKSTOP_SECS}s (esperado ≤300s)"
        )

    def test_lock_recente_é_valido(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock criado agora deve ser considerado válido (não stale)."""
        lock = tmp_path / "dashboard_esteira-repo-1.jsonl.lock"
        lock.touch()
        assert not _lock_is_stale(str(lock))

    def test_lock_antigo_é_stale(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock com age > backstop deve ser stale."""
        lock = tmp_path / "dashboard_esteira-repo-1.jsonl.lock"
        lock.touch()
        old_ts = time.time() - (_DISPATCH_BACKSTOP_SECS + 10)
        import os
        os.utime(str(lock), (old_ts, old_ts))
        assert _lock_is_stale(str(lock))

    def test_arquivo_inexistente_é_stale(self) -> None:
        """Arquivo que não existe deve ser considerado stale."""
        assert _lock_is_stale("/nonexistent/path/lock.lock")


# ---------------------------------------------------------------------------
# _issue_has_active_session — mecanismo primário de concorrência
# ---------------------------------------------------------------------------

class TestIssueHasActiveSession:
    """Testa a decisão de concorrência por estado da issue."""

    def test_worktree_presente_indica_ativo(self, tmp_path: Path) -> None:
        """Se o worktree existir, a sessão é considerada ativa."""
        wt_path = tmp_path / ".esteira-worktrees" / "repo-42"
        wt_path.mkdir(parents=True)
        dev_root = str(tmp_path)

        with mock.patch("deployment.deployment._worktree_path", return_value=str(wt_path)):
            result = _issue_has_active_session("owner/repo", 42, dev_root)

        assert result is True

    def test_pr_aberto_indica_ativo(self, tmp_path: Path) -> None:
        """Se há PR aberto na branch, a sessão é considerada ativa."""
        dev_root = str(tmp_path)

        with (
            mock.patch("deployment.deployment._worktree_path",
                       return_value=str(tmp_path / ".esteira-worktrees" / "repo-42")),
            mock.patch("deployment.deployment._pr_exists", return_value=True),
        ):
            result = _issue_has_active_session("owner/repo", 42, dev_root)

        assert result is True

    def test_backstop_lock_ativo_indica_ativo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock de backstop recente é o último recurso anti-duplo-dispatch."""
        dev_root = str(tmp_path)
        lock = tmp_path / "dashboard_esteira-repo-42.jsonl.lock"
        lock.touch()

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        with (
            mock.patch("deployment.deployment._worktree_path",
                       return_value=str(tmp_path / ".esteira-worktrees" / "repo-42")),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
        ):
            result = _issue_has_active_session("owner/repo", 42, dev_root)

        assert result is True

    def test_sem_sinais_indica_inativo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem worktree, sem PR, sem lock → sessão inativa (deve despachar)."""
        dev_root = str(tmp_path)
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        with mock.patch("deployment.deployment._pr_exists", return_value=False):
            result = _issue_has_active_session("owner/repo", 99, dev_root)

        assert result is False

    def test_backstop_stale_nao_bloqueia(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lock stale (backstop expirado) não bloqueia novo dispatch."""
        dev_root = str(tmp_path)
        lock = tmp_path / "dashboard_esteira-repo-42.jsonl.lock"
        lock.touch()
        old_ts = time.time() - (_DISPATCH_BACKSTOP_SECS + 60)
        import os
        os.utime(str(lock), (old_ts, old_ts))

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        with mock.patch("deployment.deployment._pr_exists", return_value=False):
            result = _issue_has_active_session("owner/repo", 42, dev_root)

        assert result is False


# ---------------------------------------------------------------------------
# _is_dead_session — detector de morte por timeout longo
# ---------------------------------------------------------------------------

class TestIsDeadSession:
    """Testa a detecção de sessão morta."""

    def test_constante_timeout_é_longa(self) -> None:
        """DEAD_SESSION_TIMEOUT_SECS deve ser maior que qualquer sessão legítima."""
        assert DEAD_SESSION_TIMEOUT_SECS >= 30 * 60, (
            f"timeout muito curto: {DEAD_SESSION_TIMEOUT_SECS}s (esperado ≥1800s)"
        )

    def test_running_since_none_é_fail_closed(self, tmp_path: Path) -> None:
        """Sem timestamp de running_since → fail-closed (não considera morta)."""
        result = _is_dead_session(
            "owner/repo", 42, str(tmp_path),
            running_since_secs=None,
        )
        assert result is False

    def test_dentro_do_timeout_nao_é_morte(self, tmp_path: Path) -> None:
        """Running há menos que o timeout → não é morte."""
        running_since = time.time() - (DEAD_SESSION_TIMEOUT_SECS - 60)
        result = _is_dead_session(
            "owner/repo", 42, str(tmp_path),
            running_since_secs=running_since,
        )
        assert result is False

    def test_timeout_expirado_com_worktree_não_é_morte(self, tmp_path: Path) -> None:
        """Timeout expirado mas worktree existe → sessão ainda ativa."""
        running_since = time.time() - (DEAD_SESSION_TIMEOUT_SECS + 600)

        wt_path = tmp_path / ".esteira-worktrees" / "repo-42"
        wt_path.mkdir(parents=True)

        with mock.patch("deployment.deployment._worktree_path", return_value=str(wt_path)):
            result = _is_dead_session(
                "owner/repo", 42, str(tmp_path),
                running_since_secs=running_since,
            )

        assert result is False

    def test_timeout_expirado_com_pr_aberto_não_é_morte(
        self, tmp_path: Path
    ) -> None:
        """Timeout expirado mas PR aberto → sessão ainda ativa."""
        running_since = time.time() - (DEAD_SESSION_TIMEOUT_SECS + 600)

        with (
            mock.patch("deployment.deployment._worktree_path",
                       return_value=str(tmp_path / ".esteira-worktrees" / "repo-42")),
            mock.patch("deployment.deployment._pr_exists", return_value=True),
        ):
            result = _is_dead_session(
                "owner/repo", 42, str(tmp_path),
                running_since_secs=running_since,
            )

        assert result is False

    def test_timeout_expirado_sem_sinais_é_morte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Timeout expirado sem PR, sem worktree, sem lock → sessão morta."""
        running_since = time.time() - (DEAD_SESSION_TIMEOUT_SECS + 600)

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        with (
            mock.patch("deployment.deployment._worktree_path",
                       return_value=str(tmp_path / ".esteira-worktrees" / "repo-42")),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
        ):
            result = _is_dead_session(
                "owner/repo", 42, str(tmp_path),
                running_since_secs=running_since,
            )

        assert result is True

    def test_timeout_expirado_com_backstop_lock_não_é_morte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Timeout expirado mas backstop lock ativo → ainda incerto (não considera morta)."""
        running_since = time.time() - (DEAD_SESSION_TIMEOUT_SECS + 600)

        lock = tmp_path / "dashboard_esteira-repo-42.jsonl.lock"
        lock.touch()  # lock recente

        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        with (
            mock.patch("deployment.deployment._worktree_path",
                       return_value=str(tmp_path / ".esteira-worktrees" / "repo-42")),
            mock.patch("deployment.deployment._pr_exists", return_value=False),
        ):
            result = _is_dead_session(
                "owner/repo", 42, str(tmp_path),
                running_since_secs=running_since,
            )

        assert result is False


# ---------------------------------------------------------------------------
# running_since no cache — rastreamento de quando running foi visto
# ---------------------------------------------------------------------------

class TestRunningSinceCache:
    """Testa as funções de rastreamento de running_since."""

    def test_set_e_get_running_since(self, tmp_path: Path) -> None:
        """set_running_since persiste o timestamp; get o recupera."""
        conn = open_cache("test", data_dir=tmp_path)
        try:
            from flow.scan.cache import set_hash
            set_hash(conn, "issue-key-1", "abc123")

            ts = "2026-09-18T21:00:00+00:00"
            set_running_since(conn, "issue-key-1", ts)
            result = get_running_since(conn, "issue-key-1")
        finally:
            conn.close()

        assert result == ts

    def test_set_running_since_nao_sobrescreve(self, tmp_path: Path) -> None:
        """set_running_since só grava se running_since for NULL (preserva o original)."""
        conn = open_cache("test", data_dir=tmp_path)
        try:
            from flow.scan.cache import set_hash
            set_hash(conn, "issue-key-2", "abc")

            ts_original = "2026-09-18T10:00:00+00:00"
            ts_novo = "2026-09-18T21:00:00+00:00"
            set_running_since(conn, "issue-key-2", ts_original)
            set_running_since(conn, "issue-key-2", ts_novo)

            result = get_running_since(conn, "issue-key-2")
        finally:
            conn.close()

        # Deve manter o timestamp original
        assert result == ts_original

    def test_clear_running_since(self, tmp_path: Path) -> None:
        """clear_running_since apaga o timestamp."""
        conn = open_cache("test", data_dir=tmp_path)
        try:
            from flow.scan.cache import set_hash
            set_hash(conn, "issue-key-3", "abc")

            set_running_since(conn, "issue-key-3", "2026-09-18T10:00:00+00:00")
            clear_running_since(conn, "issue-key-3")
            result = get_running_since(conn, "issue-key-3")
        finally:
            conn.close()

        assert result is None

    def test_get_running_since_retorna_none_para_chave_inexistente(
        self, tmp_path: Path
    ) -> None:
        """get_running_since retorna None para chave não registrada."""
        conn = open_cache("test", data_dir=tmp_path)
        try:
            result = get_running_since(conn, "nao-existe")
        finally:
            conn.close()

        assert result is None

    def test_migracao_cache_existente(self, tmp_path: Path) -> None:
        """open_cache migra um banco existente sem coluna running_since."""
        # Cria banco sem a coluna running_since
        db_path = tmp_path / "scan_cache_migtest.db"
        conn_old = sqlite3.connect(str(db_path))
        conn_old.execute("""
            CREATE TABLE IF NOT EXISTS issue_cache (
                key         TEXT PRIMARY KEY,
                labels_hash TEXT NOT NULL,
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        conn_old.execute("INSERT INTO issue_cache (key, labels_hash) VALUES ('k1', 'h1')")
        conn_old.commit()
        conn_old.close()

        # Reabre com open_cache — deve aplicar migração sem erro
        conn = open_cache("migtest", data_dir=tmp_path)
        try:
            # Verifica que a coluna existe e é usável
            set_running_since(conn, "k1", "2026-09-18T10:00:00+00:00")
            result = get_running_since(conn, "k1")
        finally:
            conn.close()

        assert result == "2026-09-18T10:00:00+00:00"


# ---------------------------------------------------------------------------
# Transição por estado — dispatch orientado ao estado
# ---------------------------------------------------------------------------

class TestDispatchOrientadoAoEstado:
    """run_dev usa estado da issue (labels) como mecanismo primário de concorrência."""

    def test_dispatch_quando_sem_sinais_de_sessao_ativa(self) -> None:
        """Issue em todo sem worktree/PR/lock → deve ser despachada."""
        ctx = _make_ctx()
        result = _make_scan_result("flow:develop-waiting", issue_number=42)

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since", return_value=None),
            mock.patch("deployment.deployment._is_dead_session", return_value=False),
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_called_once()

    def test_nao_despacha_quando_sessao_ativa_por_worktree(self) -> None:
        """Issue em todo mas com worktree existente → não despacha (sessão ativa)."""
        ctx = _make_ctx()
        result = _make_scan_result("flow:develop-waiting", issue_number=42)

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=True),  # sessão ativa detectada por worktree/PR
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since", return_value=None),
            mock.patch("deployment.deployment._is_dead_session", return_value=False),
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_not_called()

    def test_nao_despacha_quando_sessao_ativa_por_pr(self) -> None:
        """Issue em todo com PR aberto na branch → não despacha (anti-duplo-dispatch)."""
        ctx = _make_ctx()
        result = _make_scan_result("flow:develop-waiting", issue_number=42)

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=True),  # PR aberto detectado
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since", return_value=None),
            mock.patch("deployment.deployment._is_dead_session", return_value=False),
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_not_called()

    def test_duas_issues_diferentes_podem_ser_despachadas(self) -> None:
        """Duas issues em todo sem sessões ativas → ambas despachadas (dentro do cap)."""
        ctx = _make_ctx()
        result1 = _make_scan_result("flow:develop-waiting", issue_number=42)
        result2 = _make_scan_result("flow:develop-waiting", issue_number=43)

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(max_concurrent_tasks=2)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[result1, result2]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since", return_value=None),
            mock.patch("deployment.deployment._is_dead_session", return_value=False),
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        assert mock_dispatch.call_count == 2


# ---------------------------------------------------------------------------
# Sessão morta — detecção e recuperação
# ---------------------------------------------------------------------------

class TestRecuperacaoSessaoMorta:
    """Testa detecção e recuperação de sessão morta em run_dev."""

    def test_sessao_morta_notifica_sem_redespachar(self) -> None:
        """Sessão morta detectada → remove running, notifica TL, NÃO redespacha."""
        ctx = _make_ctx()
        # Issue em flow:develop-running + flow:develop-running (sessão deveria estar em andamento)
        result = _make_scan_result(
            "flow:develop-running",
            modifiers=["flow:develop-running"],
            issue_number=42,
        )
        # running há mais que o timeout
        running_since = time.time() - (DEAD_SESSION_TIMEOUT_SECS + 600)

        import datetime

        running_iso = datetime.datetime.fromtimestamp(
            running_since, tz=datetime.UTC
        ).isoformat()

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since",
                       return_value=running_iso),
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=False),  # sem sinais de vida
            mock.patch("deployment.deployment._is_dead_session",
                       return_value=True),  # confirmado morta
            mock.patch("deployment.deployment._recover_dead_session") as mock_recover,
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_provider = mock.MagicMock()
            mock_pf.return_value = mock_provider
            run_dev(ctx)

        # Deve ter tentado recuperar
        mock_recover.assert_called_once()
        # NÃO deve ter despachado uma nova sessão
        mock_dispatch.assert_not_called()

    def test_sessao_ativa_nao_é_recuperada(self) -> None:
        """Issue em dev + running com worktree presente → NÃO é morta, não recupera."""
        ctx = _make_ctx()
        result = _make_scan_result(
            "flow:develop-running",
            modifiers=["flow:develop-running"],
            issue_number=42,
        )

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config()),
            mock.patch("deployment.deployment.scan_candidates", return_value=[result]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since",
                       return_value="2026-09-18T10:00:00+00:00"),
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=True),  # sessão ativa (worktree ou PR presente)
            mock.patch("deployment.deployment._is_dead_session",
                       return_value=False),  # não é morte (sinais de vida)
            mock.patch("deployment.deployment._recover_dead_session") as mock_recover,
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_recover.assert_not_called()

    def test_recover_dead_session_remove_label_running(self) -> None:
        """_recover_dead_session remove flow:develop-running da issue via provider."""
        ctx = _make_ctx()
        conn = sqlite3.connect(":memory:")

        mock_provider = mock.MagicMock()
        mock_provider.get_work_item.return_value = {
            "labels": ["flow:develop-running", "flow:feature", "flow:p1"]
        }

        with mock.patch(
            "deployment.deployment.clear_running_since"
        ) as mock_clear:
            _recover_dead_session(
                ctx, "owner/repo", 42, mock_provider, "chat-123", conn
            )

        # remove flow:develop-running
        mock_provider.set_labels.assert_called_once()
        _call = mock_provider.set_labels.call_args
        labels_after = _call[0][2]  # terceiro argumento posicional
        assert "flow:develop-running" not in labels_after
        # Preserva outras labels
        assert "flow:feature" in labels_after

        # limpa running_since no cache
        mock_clear.assert_called_once()

        # notifica
        ctx.notify.assert_called_once()
        msg = ctx.notify.call_args[0][0]
        assert "morta" in msg.lower() or "sessão" in msg.lower()

    def test_recover_dead_session_fail_closed_em_erro(self) -> None:
        """_recover_dead_session não propaga exceção — notifica sobre falha."""
        ctx = _make_ctx()
        conn = sqlite3.connect(":memory:")

        mock_provider = mock.MagicMock()
        mock_provider.get_work_item.side_effect = RuntimeError("API error")

        _recover_dead_session(
            ctx, "owner/repo", 42, mock_provider, "", conn
        )

        # Deve ter notificado sobre a falha, não explodido
        ctx.notify.assert_called_once()
        msg = ctx.notify.call_args[0][0]
        assert "morta" in msg.lower() or "sessão" in msg.lower()

        conn.close()


# ---------------------------------------------------------------------------
# Anti-duplo-dispatch — fail-closed em ambiguidade
# ---------------------------------------------------------------------------

class TestAntiDuploDispatch:
    """Verifica o comportamento fail-closed quando há ambiguidade de estado."""

    def test_cap_de_concorrência_por_running_count(self) -> None:
        """Cap é decidido pelo número de issues em dev+running, não só por locks."""
        ctx = _make_ctx()
        # Duas issues em dev+running no scan (cap=2)
        running1 = _make_scan_result(
            "flow:develop-running", modifiers=["flow:develop-running"], issue_number=10
        )
        running2 = _make_scan_result(
            "flow:develop-running", modifiers=["flow:develop-running"], issue_number=11
        )
        # Uma issue em todo (candidata ao dispatch)
        todo = _make_scan_result("flow:develop-waiting", issue_number=12)

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(max_concurrent_tasks=2)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[running1, running2, todo]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=False),
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since", return_value=None),
            mock.patch("deployment.deployment._is_dead_session", return_value=False),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        # Cap atingido por running_dev_count=2 → issue todo deve ficar na fila
        mock_dispatch.assert_not_called()

    def test_cap_usa_max_running_e_backstop(self) -> None:
        """Cap usa max(running_dev_count, backstop_count) para ser conservador."""
        ctx = _make_ctx()
        # 1 issue em running no scan, mas 2 locks ativos (dispatch recente)
        running1 = _make_scan_result(
            "flow:develop-running", modifiers=["flow:develop-running"], issue_number=10
        )
        todo = _make_scan_result("flow:develop-waiting", issue_number=11)

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(max_concurrent_tasks=2)),
            mock.patch("deployment.deployment.scan_candidates",
                       return_value=[running1, todo]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions",
                       return_value=2),  # 2 backstops ativos
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=False),
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since", return_value=None),
            mock.patch("deployment.deployment._is_dead_session", return_value=False),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        # max(1, 2) = 2 = cap → não despacha
        mock_dispatch.assert_not_called()

    def test_sem_running_issues_pode_despachar_até_cap(self) -> None:
        """Sem issues em running e sem backstop ativo → pode despachar até o cap."""
        ctx = _make_ctx()
        todo = _make_scan_result("flow:develop-waiting", issue_number=5)

        with (
            mock.patch("deployment.deployment._load_config",
                       return_value=_base_config(max_concurrent_tasks=2)),
            mock.patch("deployment.deployment.scan_candidates", return_value=[todo]),
            mock.patch("deployment.deployment.open_cache") as mock_cache,
            mock.patch("deployment.deployment.provider_for") as mock_pf,
            mock.patch("deployment.deployment._active_sessions", return_value=0),
            mock.patch("deployment.deployment._issue_has_active_session",
                       return_value=False),
            mock.patch("deployment.deployment._resource_headroom_ok", return_value=True),
            mock.patch("deployment.deployment._clean_stale_worktree"),
            mock.patch("deployment.deployment._dispatch") as mock_dispatch,
            mock.patch("deployment.deployment.set_running_since"),
            mock.patch("deployment.deployment.clear_running_since"),
            mock.patch("deployment.deployment.get_running_since", return_value=None),
            mock.patch("deployment.deployment._is_dead_session", return_value=False),
        ):
            mock_cache.return_value = sqlite3.connect(":memory:")
            mock_pf.return_value = mock.MagicMock()
            run_dev(ctx)

        mock_dispatch.assert_called_once()

    def test_repo_has_active_ainda_funciona_como_backstop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_repo_has_active ainda existe como backstop (NÃO foi removida)."""
        lock = tmp_path / "dashboard_esteira-myrepo-99.jsonl.lock"
        lock.touch()
        monkeypatch.setattr("deployment.deployment._sessdir", lambda: str(tmp_path))

        result = _repo_has_active("owner/myrepo")
        assert result is True
