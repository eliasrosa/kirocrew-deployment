"""Testes para flow.prompts.loader — loader de templates MD para sessões one-shot."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

# Garante que o repo root está no path (como deployment.py faz em produção)
_REPO_ROOT = str(Path(__file__).parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest  # noqa: E402

from flow.prompts.loader import PromptRenderError, render_prompt  # noqa: E402

# ---------------------------------------------------------------------------
# Interpolação OK
# ---------------------------------------------------------------------------

class TestRenderPromptInterpolation:
    def test_substitui_variavel_simples(self) -> None:
        tpl = "REPO: {{repo}}"
        result = render_prompt("__test_nonexistent__", fallback=tpl, repo="owner/repo")
        assert result == "REPO: owner/repo"

    def test_substitui_multiplas_variaveis(self) -> None:
        tpl = "{{a}} + {{b}} = {{c}}"
        result = render_prompt("__test_nonexistent__", fallback=tpl, a="1", b="2", c="3")
        assert result == "1 + 2 = 3"

    def test_variavel_repetida_substituida_todas_as_ocorrencias(self) -> None:
        tpl = "{{x}} e {{x}} e {{x}}"
        result = render_prompt("__test_nonexistent__", fallback=tpl, x="ok")
        assert result == "ok e ok e ok"

    def test_sem_placeholders_retorna_template_intacto(self) -> None:
        tpl = "sem nenhuma variável aqui"
        result = render_prompt("__test_nonexistent__", fallback=tpl)
        assert result == tpl

    def test_variavel_extra_ignorada(self) -> None:
        """Variáveis fornecidas mas não usadas no template são silenciosamente ignoradas."""
        tpl = "REPO: {{repo}}"
        result = render_prompt("__test_nonexistent__", fallback=tpl, repo="owner/repo", extra_unused="x")
        assert result == "REPO: owner/repo"


# ---------------------------------------------------------------------------
# Variável faltando → fail-closed
# ---------------------------------------------------------------------------

class TestRenderPromptMissingVariable:
    def test_variavel_faltando_levanta_prompt_render_error(self) -> None:
        tpl = "REPO: {{repo}} ISSUE: {{issue_number}}"
        with pytest.raises(PromptRenderError) as exc_info:
            render_prompt("__test_nonexistent__", fallback=tpl, repo="owner/repo")
            # issue_number não fornecido
        assert "issue_number" in str(exc_info.value)

    def test_multiplas_variaveis_faltando_todas_reportadas(self) -> None:
        tpl = "{{a}} {{b}} {{c}}"
        with pytest.raises(PromptRenderError) as exc_info:
            render_prompt("__test_nonexistent__", fallback=tpl)
        msg = str(exc_info.value)
        assert "a" in msg
        assert "b" in msg
        assert "c" in msg

    def test_prompt_render_error_e_subclasse_de_exception(self) -> None:
        assert issubclass(PromptRenderError, Exception)


# ---------------------------------------------------------------------------
# Template ausente no disco → fallback
# ---------------------------------------------------------------------------

class TestRenderPromptFallback:
    def test_template_ausente_usa_fallback(self, tmp_path: Path) -> None:
        """Quando o MD não existe no disco, usa o fallback sem levantar erro."""
        fallback = "FALLBACK: {{repo}}"
        # Aponta o loader para um diretório vazio — arquivo não existirá
        with mock.patch("flow.prompts.loader._PROMPTS_DIR", tmp_path):
            result = render_prompt("dev", fallback=fallback, repo="owner/repo")
        assert result == "FALLBACK: owner/repo"

    def test_template_ausente_sem_fallback_levanta_file_not_found(
        self, tmp_path: Path
    ) -> None:
        with mock.patch("flow.prompts.loader._PROMPTS_DIR", tmp_path), pytest.raises(FileNotFoundError):
            render_prompt("dev")  # sem fallback

    def test_template_corrompido_ilegivel_usa_fallback(self, tmp_path: Path) -> None:
        """Arquivo existe mas não pode ser lido (ex: permissão) → usa fallback."""
        stage_file = tmp_path / "dev.md"
        stage_file.write_text("{{repo}}", encoding="utf-8")
        stage_file.chmod(0o000)  # sem leitura

        fallback = "FALLBACK: {{repo}}"
        try:
            with mock.patch("flow.prompts.loader._PROMPTS_DIR", tmp_path):
                result = render_prompt("dev", fallback=fallback, repo="ok")
            assert result == "FALLBACK: ok"
        finally:
            stage_file.chmod(0o644)  # restaura para não sujar o diretório


# ---------------------------------------------------------------------------
# Template em disco preferido sobre fallback
# ---------------------------------------------------------------------------

class TestRenderPromptDiskTemplate:
    def test_template_em_disco_tem_prioridade_sobre_fallback(
        self, tmp_path: Path
    ) -> None:
        disk_tpl = "DISK: {{repo}}"
        fallback_tpl = "FALLBACK: {{repo}}"
        (tmp_path / "dev.md").write_text(disk_tpl, encoding="utf-8")

        with mock.patch("flow.prompts.loader._PROMPTS_DIR", tmp_path):
            result = render_prompt("dev", fallback=fallback_tpl, repo="owner/repo")
        assert result == "DISK: owner/repo"
        assert "FALLBACK" not in result

    def test_template_em_disco_valida_variaveis(self, tmp_path: Path) -> None:
        """Template em disco com variável faltando também levanta PromptRenderError."""
        (tmp_path / "dev.md").write_text("{{repo}} {{missing}}", encoding="utf-8")

        with mock.patch("flow.prompts.loader._PROMPTS_DIR", tmp_path), pytest.raises(PromptRenderError) as exc_info:
            render_prompt("dev", fallback=None, repo="owner/repo")
        assert "missing" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Integração com os templates reais do repositório
# ---------------------------------------------------------------------------

class TestRealTemplates:
    """Smoke tests — confirma que os templates versionados renderizam sem erro."""

    def test_dev_template_real_renderiza_sem_erro(self) -> None:
        result = render_prompt(
            "dev",
            repo="owner/myrepo",
            repo_short="myrepo",
            issue_number="42",
            issue_title="Fix bug",
            issue_url="https://github.com/owner/myrepo/issues/42",
            session_title="myrepo #42: Fix bug",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/myrepo-42",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="",
        )
        assert "owner/myrepo" in result
        assert "#42" in result
        assert "Fix bug" in result
        assert "NUNCA mergeie" in result
        assert "crewflow:dev" in result
        assert "crewflow:blocked" in result

    def test_dev_template_real_contem_worktree_path(self) -> None:
        result = render_prompt(
            "dev",
            repo="owner/myrepo",
            repo_short="myrepo",
            issue_number="99",
            issue_title="Feat",
            issue_url="https://github.com/owner/myrepo/issues/99",
            session_title="myrepo #99: Feat",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/myrepo-99",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="",
        )
        assert "/home/dev/.esteira-worktrees/myrepo-99" in result

    def test_dev_template_real_com_vault_step(self) -> None:
        result = render_prompt(
            "dev",
            repo="owner/myrepo",
            repo_short="myrepo",
            issue_number="1",
            issue_title="T",
            issue_url="https://github.com/owner/myrepo/issues/1",
            session_title="myrepo #1: T",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/myrepo-1",
            notify_step="reporte o resultado, ",
            vault_step="   - VAULT: edite /vault/backlog.md",
            prompt_extra="",
        )
        assert "VAULT" in result

    def test_dev_template_real_com_prompt_extra(self) -> None:
        result = render_prompt(
            "dev",
            repo="owner/myrepo",
            repo_short="myrepo",
            issue_number="1",
            issue_title="T",
            issue_url="https://github.com/owner/myrepo/issues/1",
            session_title="myrepo #1: T",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/myrepo-1",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="instrução extra aqui",
        )
        assert "instrução extra aqui" in result

    def test_dev_template_real_le_comentarios_da_issue(self) -> None:
        """Passo 1 (CONTEXTO) deve instruir a leitura dos comentários da issue."""
        result = render_prompt(
            "dev",
            repo="owner/myrepo",
            repo_short="myrepo",
            issue_number="42",
            issue_title="Fix bug",
            issue_url="https://github.com/owner/myrepo/issues/42",
            session_title="myrepo #42: Fix bug",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/myrepo-42",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="",
        )
        assert "gh issue view 42 --repo owner/myrepo --comments" in result

    def test_reviewer_template_real_renderiza_sem_erro(self) -> None:
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="5",
            issue_number="42",
            head_sha="abc1234def5678",
            example_approved="     ## 🤖 KiroCrew Review\n     **Resultado:** ✅ Aprovado",
            example_changes=(
                "     ## 🤖 KiroCrew Review\n"
                "     **Resultado:** ⚠️ Pedidos de mudança"
            ),
        )
        assert "owner/myrepo" in result
        assert "#5" in result
        assert "#42" in result
        assert "NUNCA mergeie" in result
        assert "crewflow:review-ok" in result

    def test_reviewer_template_real_session_title(self) -> None:
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="10",
            issue_number="20",
            head_sha="0000000",
            example_approved="",
            example_changes="",
        )
        assert "review: myrepo PR #10 (issue #20)" in result

    def test_reviewer_template_real_le_comentarios_issue_e_pr(self) -> None:
        """Passos 1-2 devem instruir a leitura dos comentários da issue e do PR."""
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="5",
            issue_number="42",
            head_sha="abc1234",
            example_approved="",
            example_changes="",
        )
        assert "gh issue view 42 --repo owner/myrepo --comments" in result
        assert "gh pr view 5 --repo owner/myrepo --comments" in result

    def test_reviewer_template_verifica_ci(self) -> None:
        """Passo 4: reviewer deve verificar o status da pipeline de CI."""
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="5",
            issue_number="42",
            head_sha="abc1234",
            example_approved="",
            example_changes="",
        )
        assert "gh pr checks 5 --repo owner/myrepo" in result
        assert "CI" in result

    def test_reviewer_template_ci_vermelho_bloqueia(self) -> None:
        """CI vermelho deve bloquear aprovação mesmo que o código esteja correto."""
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="5",
            issue_number="42",
            head_sha="abc1234",
            example_approved="",
            example_changes="",
        )
        assert "CI vermelho" in result or "failure" in result or "bloqueio" in result

    def test_reviewer_template_posta_nos_dois_lugares(self) -> None:
        """Resultado completo deve ser postado tanto no PR quanto na issue."""
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="5",
            issue_number="42",
            head_sha="abc1234",
            example_approved="",
            example_changes="",
        )
        # Posta no PR
        assert "POSTE O RESULTADO COMPLETO DO REVIEW NO PR" in result
        # Posta na issue também (completo, não só referência curta)
        assert "POSTE O RESULTADO COMPLETO DO REVIEW NA ISSUE" in result
        assert "gh issue comment" in result

    def test_reviewer_template_aprovado_somente_com_ci_verde_e_sem_comentarios(self) -> None:
        """A decisão de aprovação deve exigir CI verde + zero comentários."""
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="5",
            issue_number="42",
            head_sha="abc1234",
            example_approved="",
            example_changes="",
        )
        # A decisão exige as três condições
        assert "CI verde" in result
        assert "crewflow:review-ok" in result

    def test_reviewer_template_contem_head_sha_injetado(self) -> None:
        """O prompt deve incluir o SHA injetado para que o reviewer saiba qual HEAD revisar."""
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="5",
            issue_number="42",
            head_sha="deadbeef1234",
            example_approved="",
            example_changes="",
        )
        assert "deadbeef1234" in result

    def test_reviewer_template_instrui_confirmar_sha_atual(self) -> None:
        """O reviewer deve confirmar o SHA atual via gh pr view --json headRefOid."""
        result = render_prompt(
            "reviewer",
            repo="owner/myrepo",
            repo_short="myrepo",
            pr_number="5",
            issue_number="42",
            head_sha="abc1234",
            example_approved="",
            example_changes="",
        )
        assert "headRefOid" in result
        assert "gh pr view 5 --repo owner/myrepo --json headRefOid" in result
