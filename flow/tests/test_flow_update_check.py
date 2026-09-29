"""Testes da casca de I/O do cron ``scripts/flow_update_check.py``.

A lógica de decisão pura vive em ``flow/domain/update_policy.py`` (coberta por
``test_domain_update_policy.py``). Aqui testamos a **casca de I/O** que envolve
a decisão — exatamente o lugar onde vivia o defeito do review v1: o caminho
``AUTO_UPDATE`` reinstalava o checkout atual sem nunca fazer ``git checkout`` da
versão alvo, reportando um "sucesso" que não movia o código.

Os testes mockam ``subprocess`` e o sistema de arquivos (via monkeypatch das
funções helper do módulo), então rodam sem git nem rede. As garantias centrais:

- ``AUTO_UPDATE`` (patch) faz ``git checkout <vX.Y.Z>`` **antes** de reinstalar,
  e só reporta sucesso se a versão reconfirmada avançou.
- ``NOTIFY_ONLY`` (minor/major) **apenas notifica** — nenhuma ação de update.
- ``NOOP`` (igual/mais novo) faz ``Skip`` sem tocar em nada.
- Uma falha de ``git fetch --tags`` resulta em ``Skip`` (NOOP duro), nunca
  decidindo sobre tags obsoletas.
"""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Carrega scripts/flow_update_check.py como módulo (não é um pacote instalável).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "flow_update_check.py"


def _load_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "flow_update_check_under_test", _SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


upd = _load_module()


# ---------------------------------------------------------------------------
# Helpers de teste
# ---------------------------------------------------------------------------

def _run_capturing_sentinel(ctx: object = object()):
    """Roda ``upd.run`` capturando o sentinel (Skip/Report/Done) levantado."""
    try:
        upd.run(ctx)
    except (upd.Skip, upd.Report, upd.Done) as sentinel:
        return sentinel
    raise AssertionError("run() não levantou nenhum sentinel")


@pytest.fixture
def repo(monkeypatch):
    """Faz ``_find_repo_root`` retornar um path fixo."""
    monkeypatch.setattr(upd, "_find_repo_root", lambda: "/fake/repo")
    return "/fake/repo"


# ---------------------------------------------------------------------------
# NOOP: igual ou mais novo → Skip sem ação
# ---------------------------------------------------------------------------

class TestNoop:
    def test_versao_igual_skip(self, repo, monkeypatch):
        monkeypatch.setattr(upd, "_installed_version", lambda r: "1.2.3")
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.2.3")
        # Se alguém tentar atualizar, falhamos alto.
        monkeypatch.setattr(
            upd, "_run_official_update",
            lambda *a, **k: pytest.fail("NOOP não deve atualizar"),
        )
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Skip)

    def test_instalado_mais_novo_skip(self, repo, monkeypatch):
        monkeypatch.setattr(upd, "_installed_version", lambda r: "1.5.0")
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.4.9")
        monkeypatch.setattr(
            upd, "_run_official_update",
            lambda *a, **k: pytest.fail("NOOP não deve atualizar"),
        )
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Skip)


# ---------------------------------------------------------------------------
# NOTIFY_ONLY: minor/major → apenas Report, nenhuma ação de update
# ---------------------------------------------------------------------------

class TestNotifyOnly:
    def test_minor_apenas_notifica(self, repo, monkeypatch):
        monkeypatch.setattr(upd, "_installed_version", lambda r: "1.2.3")
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.3.0")
        monkeypatch.setattr(
            upd, "_run_official_update",
            lambda *a, **k: pytest.fail("minor NUNCA pode auto-aplicar"),
        )
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Report)
        assert "1.2.3" in sentinel.msg and "1.3.0" in sentinel.msg

    def test_major_apenas_notifica(self, repo, monkeypatch):
        monkeypatch.setattr(upd, "_installed_version", lambda r: "1.2.3")
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v2.0.0")
        monkeypatch.setattr(
            upd, "_run_official_update",
            lambda *a, **k: pytest.fail("major NUNCA pode auto-aplicar"),
        )
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Report)

    def test_notificacao_manual_manda_fazer_checkout_da_tag(self, repo, monkeypatch):
        """A mensagem manual deve mandar avançar o código (checkout da tag),
        não apenas rodar install-cron.sh (que reinstalaria o checkout atual)."""
        monkeypatch.setattr(upd, "_installed_version", lambda r: "1.2.3")
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.3.0")
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Report)
        assert "checkout" in sentinel.msg.lower()
        assert "v1.3.0" in sentinel.msg


# ---------------------------------------------------------------------------
# AUTO_UPDATE (patch): DEVE avançar o working tree antes de reinstalar
# ---------------------------------------------------------------------------

class TestAutoUpdateAdvancesCode:
    def test_patch_faz_checkout_antes_de_instalar(self, repo, monkeypatch):
        """O teste-âncora do defeito: AUTO_UPDATE precisa fazer
        git checkout <tag> ANTES do install-cron.sh, e confirmar o avanço."""
        calls: list[str] = []

        # installed é lida duas vezes: antes (1.2.3) e na reconfirmação (1.2.4).
        installed_seq = iter(["1.2.3", "1.2.4"])
        monkeypatch.setattr(
            upd, "_installed_version", lambda r: next(installed_seq)
        )
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.2.4")

        def fake_checkout(repo_root, tag):
            calls.append(f"checkout:{tag}")

        def fake_install(repo_root):
            calls.append("install")

        monkeypatch.setattr(upd, "_checkout_target", fake_checkout)
        monkeypatch.setattr(upd, "_run_install", fake_install)

        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Report)
        # Ordem obrigatória: checkout da tag alvo ANTES da reinstalação.
        assert calls == ["checkout:v1.2.4", "install"], calls
        assert "auto-atualizado" in sentinel.msg.lower()

    def test_patch_reporta_falha_se_versao_nao_avancou(self, repo, monkeypatch):
        """Se após checkout+install a versão não avançou, reporta aviso em vez
        de anunciar um sucesso falso."""
        # Sempre reporta 1.2.3 → nunca avançou para 1.2.4.
        monkeypatch.setattr(upd, "_installed_version", lambda r: "1.2.3")
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.2.4")
        monkeypatch.setattr(upd, "_checkout_target", lambda r, t: None)
        monkeypatch.setattr(upd, "_run_install", lambda r: None)

        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Report)
        assert "não avançou" in sentinel.msg

    def test_patch_propaga_falha_de_checkout(self, repo, monkeypatch):
        """Se o git checkout falhar, _run_official_update levanta Report e o
        install NUNCA é chamado."""
        monkeypatch.setattr(upd, "_installed_version", lambda r: "1.2.3")
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.2.4")

        def boom_checkout(r, t):
            raise upd.Report("checkout falhou")

        monkeypatch.setattr(upd, "_checkout_target", boom_checkout)
        monkeypatch.setattr(
            upd, "_run_install",
            lambda r: pytest.fail("install não deve rodar se o checkout falhou"),
        )
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Report)
        assert "checkout falhou" in sentinel.msg


# ---------------------------------------------------------------------------
# Descoberta de canal: fetch falho = NOOP duro; sem canal = Skip
# ---------------------------------------------------------------------------

class TestChannelDiscovery:
    def test_fetch_falho_resulta_em_skip(self, repo, monkeypatch):
        """_channel_version retorna None (fetch falhou) → run faz Skip, sem update."""
        monkeypatch.setattr(upd, "_installed_version", lambda r: "1.2.3")
        # Simula _channel_version devolvendo None (o que ele faz quando o fetch falha).
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: None)
        monkeypatch.setattr(
            upd, "_run_official_update",
            lambda *a, **k: pytest.fail("sem canal resolvido não deve atualizar"),
        )
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Skip)

    def test_channel_version_fetch_falha_retorna_none(self, monkeypatch):
        """_channel_version trata um git fetch --tags com rc!=0 como None."""
        class _FakeCompleted:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(cmd, **kwargs):
            # git fetch --tags falha
            assert cmd[:3] == ["git", "fetch", "--tags"]
            return _FakeCompleted(returncode=1, stderr="network down")

        monkeypatch.setattr(upd.subprocess, "run", fake_run)
        assert upd._channel_version("/fake/repo", "stable") is None

    def test_channel_version_escolhe_maior_vXYZ(self, monkeypatch):
        """Com múltiplas tags apontando o SHA, escolhe a maior vX.Y.Z e ignora
        tags não-versão (stable/latest/verify-x)."""
        class _FakeCompleted:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "fetch", "--tags"]:
                return _FakeCompleted(0)
            if cmd[:2] == ["git", "ls-remote"]:
                return _FakeCompleted(0, stdout="abc123\trefs/tags/stable\n")
            if cmd[:3] == ["git", "tag", "--points-at"]:
                # ordem arbitrária, inclui ruído
                return _FakeCompleted(
                    0,
                    stdout="stable\nlatest\nverify-x\nv1.2.4\nv1.2.10\n",
                )
            raise AssertionError(f"comando inesperado: {cmd}")

        monkeypatch.setattr(upd.subprocess, "run", fake_run)
        assert upd._channel_version("/fake/repo", "stable") == "v1.2.10"

    def test_channel_version_sem_tag_versao_retorna_none(self, monkeypatch):
        class _FakeCompleted:
            def __init__(self, returncode, stdout="", stderr=""):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(cmd, **kwargs):
            if cmd[:3] == ["git", "fetch", "--tags"]:
                return _FakeCompleted(0)
            if cmd[:2] == ["git", "ls-remote"]:
                return _FakeCompleted(0, stdout="abc123\trefs/tags/stable\n")
            if cmd[:3] == ["git", "tag", "--points-at"]:
                return _FakeCompleted(0, stdout="stable\nlatest\nverify-x\n")
            raise AssertionError(f"comando inesperado: {cmd}")

        monkeypatch.setattr(upd.subprocess, "run", fake_run)
        assert upd._channel_version("/fake/repo", "stable") is None


# ---------------------------------------------------------------------------
# Guardas de indisponibilidade → Skip
# ---------------------------------------------------------------------------

class TestUnavailableInputs:
    def test_repo_root_indisponivel_skip(self, monkeypatch):
        monkeypatch.setattr(upd, "_find_repo_root", lambda: None)
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Skip)

    def test_versao_instalada_indisponivel_skip(self, repo, monkeypatch):
        monkeypatch.setattr(upd, "_installed_version", lambda r: None)
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.0.0")
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Skip)

    def test_versao_invalida_skip(self, repo, monkeypatch):
        monkeypatch.setattr(upd, "_installed_version", lambda r: "not-a-version")
        monkeypatch.setattr(upd, "_channel_version", lambda r, c: "v1.0.0")
        sentinel = _run_capturing_sentinel()
        assert isinstance(sentinel, upd.Skip)
