"""Testes da política de update auto vs manual (issue #247, item 4).

A regra é o núcleo do princípio inviolável: só **patch** (``fix``, compatível
para trás) pode auto-aplicar; **minor/major** apenas notifica e aguarda ação
manual; versão igual ou mais nova é ``NOOP``.

Estes testes são pura lógica de domínio — sem mock, sem I/O. Eles falhariam se
a política fosse revertida para "sempre auto-atualiza" (ver
``test_minor_nunca_auto`` e ``test_major_nunca_auto``).
"""

from __future__ import annotations

import pytest

from flow.domain.update_policy import (
    UpdateAction,
    Version,
    decide_update_action,
    parse_version,
)

# ---------------------------------------------------------------------------
# parse_version
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_versao_simples(self) -> None:
        assert parse_version("1.2.3") == Version(1, 2, 3)

    def test_prefixo_v(self) -> None:
        assert parse_version("v1.0.0") == Version(1, 0, 0)

    def test_pre_release_ignorado(self) -> None:
        assert parse_version("2.1.0-rc.1") == Version(2, 1, 0)

    def test_build_metadata_ignorado(self) -> None:
        assert parse_version("1.4.2+build.9") == Version(1, 4, 2)

    def test_versao_parcial_completa_com_zeros(self) -> None:
        assert parse_version("1") == Version(1, 0, 0)
        assert parse_version("1.2") == Version(1, 2, 0)

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "v", "1.x.0"])
    def test_invalido_levanta(self, raw: str) -> None:
        with pytest.raises(ValueError):
            parse_version(raw)


# ---------------------------------------------------------------------------
# Version — ordenação
# ---------------------------------------------------------------------------

class TestVersionOrdering:
    def test_comparacao_por_trio(self) -> None:
        assert Version(1, 0, 0) < Version(1, 0, 1)
        assert Version(1, 0, 1) < Version(1, 1, 0)
        assert Version(1, 9, 9) < Version(2, 0, 0)
        assert Version(1, 2, 3) == Version(1, 2, 3)

    def test_str(self) -> None:
        assert str(Version(3, 1, 4)) == "3.1.4"


# ---------------------------------------------------------------------------
# decide_update_action — a política
# ---------------------------------------------------------------------------

class TestDecideUpdateAction:
    def test_patch_auto_update(self) -> None:
        """Patch (fix) compatível para trás → AUTO_UPDATE."""
        d = decide_update_action(Version(1, 2, 3), Version(1, 2, 4))
        assert d.action is UpdateAction.AUTO_UPDATE
        assert d.is_auto
        assert not d.is_notify

    def test_patch_multiplos_incrementos_ainda_auto(self) -> None:
        d = decide_update_action(Version(1, 2, 3), Version(1, 2, 10))
        assert d.action is UpdateAction.AUTO_UPDATE

    def test_minor_notify_only(self) -> None:
        """Minor (feat) pode mudar comportamento → NOTIFY_ONLY."""
        d = decide_update_action(Version(1, 2, 3), Version(1, 3, 0))
        assert d.action is UpdateAction.NOTIFY_ONLY
        assert d.is_notify

    def test_minor_com_patch_maior_ainda_notify(self) -> None:
        """Um minor mais novo não vira auto só porque o patch também subiu."""
        d = decide_update_action(Version(1, 2, 3), Version(1, 3, 5))
        assert d.action is UpdateAction.NOTIFY_ONLY

    def test_major_notify_only(self) -> None:
        """Major/breaking → NOTIFY_ONLY."""
        d = decide_update_action(Version(1, 2, 3), Version(2, 0, 0))
        assert d.action is UpdateAction.NOTIFY_ONLY

    def test_major_com_patch_maior_ainda_notify(self) -> None:
        d = decide_update_action(Version(1, 2, 3), Version(2, 2, 4))
        assert d.action is UpdateAction.NOTIFY_ONLY

    def test_igual_noop(self) -> None:
        d = decide_update_action(Version(1, 2, 3), Version(1, 2, 3))
        assert d.action is UpdateAction.NOOP

    def test_instalado_mais_novo_noop(self) -> None:
        """Instalado > target (ex.: rodando latest e stable ainda atrás) → NOOP."""
        d = decide_update_action(Version(1, 3, 0), Version(1, 2, 9))
        assert d.action is UpdateAction.NOOP

    def test_reason_e_versoes_preenchidos(self) -> None:
        d = decide_update_action(Version(1, 0, 0), Version(1, 0, 1))
        assert d.reason
        assert d.installed == Version(1, 0, 0)
        assert d.target == Version(1, 0, 1)


# ---------------------------------------------------------------------------
# Guarda anti-regressão: minor/major NUNCA podem auto-aplicar.
# Estes testes falham se a política for revertida para "sempre auto-update".
# ---------------------------------------------------------------------------

class TestNuncaAutoParaMudancaDeComportamento:
    @pytest.mark.parametrize(
        "installed,target",
        [
            (Version(1, 0, 0), Version(1, 1, 0)),   # minor
            (Version(1, 5, 2), Version(1, 6, 0)),   # minor
            (Version(1, 0, 0), Version(2, 0, 0)),   # major
            (Version(2, 3, 4), Version(3, 0, 0)),   # major
        ],
    )
    def test_minor_e_major_nunca_auto(
        self, installed: Version, target: Version
    ) -> None:
        d = decide_update_action(installed, target)
        assert d.action is not UpdateAction.AUTO_UPDATE, (
            "minor/major NUNCA pode auto-aplicar — violaria o princípio "
            "inviolável (produção nunca é auto-quebrada)."
        )
        assert d.action is UpdateAction.NOTIFY_ONLY
