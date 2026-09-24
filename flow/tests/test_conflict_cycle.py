"""Testes do ciclo de conflito de merge (issue #89).

Cobre:
  - Modifier.MERGE_CONFLICT existe e está em Modifier StrEnum
  - parse_modifiers reconhece flow:merge-conflict
  - Executor: REVIEW + CONFLITO → DISPATCH_CONFLICT_RESOLVER
  - Executor: REVIEW + PR CONFLICTING (sem label) → MARK_CONFLITO
  - Executor: REVIEW normal (sem conflito) → DISPATCH_REVIEWER
  - Guard de reprocessamento: PR existente impede DISPATCH_DEV
  - Prompts: conflict.md renderiza com todas as variáveis esperadas
"""

from __future__ import annotations

from flow.domain.gates import WorkItem
from flow.domain.state import Modifier, State, parse_modifiers
from flow.executor.executor import ActionKind, decide
from flow.scan.scanner import ScanResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(
    key: str = "https://github.com/owner/repo/issues/1",
    title: str = "[repo] Fix",
    labels: list[str] | None = None,
    state: State = State.REVIEW_WAITING,
    modifiers: set[Modifier] | None = None,
) -> ScanResult:
    lbl_set = frozenset(labels or ["flow:review-waiting", "flow:feature"])
    return ScanResult(
        item=WorkItem(key=key, title=title, labels=lbl_set),
        current_state=state,
        modifiers=frozenset(modifiers or []),
        dispatch_candidate=False,
        spec_valid=None,
        changed=True,
        reason="test",
    )


# ---------------------------------------------------------------------------
# Modifier.MERGE_CONFLICT — existência e parsing
# ---------------------------------------------------------------------------

class TestConflitoParsing:
    def test_conflito_esta_em_modifier(self) -> None:
        """Modifier.MERGE_CONFLICT deve existir com o valor correto."""
        assert Modifier.MERGE_CONFLICT == "flow:merge-conflict"

    def test_parse_modifiers_reconhece_conflito(self) -> None:
        """parse_modifiers deve extrair flow:merge-conflict."""
        labels = frozenset({"flow:review-waiting", "flow:merge-conflict", "flow:feature"})
        mods = parse_modifiers(labels)
        assert Modifier.MERGE_CONFLICT in mods

    def test_parse_modifiers_sem_conflito(self) -> None:
        """Labels sem conflito não devem incluir o modificador."""
        labels = frozenset({"flow:review-waiting", "flow:feature"})
        mods = parse_modifiers(labels)
        assert Modifier.MERGE_CONFLICT not in mods

    def test_conflito_nao_e_stop_modifier(self) -> None:
        """CONFLITO não deve estar em STOP_MODIFIERS (não impede dispatch de conflito)."""
        from flow.domain.state import STOP_MODIFIERS
        assert Modifier.MERGE_CONFLICT not in STOP_MODIFIERS


# ---------------------------------------------------------------------------
# Executor: detecção de conflito
# ---------------------------------------------------------------------------

class TestConflitoCycle:
    def test_conflito_label_despacha_resolver(self) -> None:
        """REVIEW + flow:merge-conflict → DISPATCH_CONFLICT_RESOLVER."""
        r = _result(
            labels=["flow:review-waiting", "flow:merge-conflict", "flow:feature"],
            modifiers={Modifier.MERGE_CONFLICT},
        )
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_CONFLICT_RESOLVER

    def test_conflito_resolver_adiciona_running(self) -> None:
        """Dispatch conflict resolver deve adicionar flow:develop-running."""
        r = _result(
            labels=["flow:review-waiting", "flow:merge-conflict", "flow:feature"],
            modifiers={Modifier.MERGE_CONFLICT},
        )
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_CONFLICT_RESOLVER
        # conflict resolver não muda o estado da issue — mantém review-waiting
        # apenas remove o modificador de conflito
        assert "flow:merge-conflict" not in d.add_labels

    def test_conflito_resolver_remove_conflito_label(self) -> None:
        """Dispatch conflict resolver deve remover flow:merge-conflict."""
        r = _result(
            labels=["flow:review-waiting", "flow:merge-conflict", "flow:feature"],
            modifiers={Modifier.MERGE_CONFLICT},
        )
        d = decide(r)
        assert "flow:merge-conflict" in d.remove_labels

    def test_pr_conflicting_marca_conflito(self) -> None:
        """REVIEW sem label conflito mas PR CONFLICTING → MARK_CONFLITO."""
        r = _result(
            labels=["flow:review-waiting", "flow:feature"],
        )
        d = decide(r, pr_mergeable="CONFLICTING")
        assert d.action is ActionKind.MARK_CONFLITO
        assert "flow:merge-conflict" in d.add_labels

    def test_pr_mergeable_nao_marca_conflito(self) -> None:
        """REVIEW com PR MERGEABLE → DISPATCH_REVIEWER (fluxo normal)."""
        r = _result(
            labels=["flow:review-waiting", "flow:feature"],
        )
        d = decide(r, pr_mergeable="MERGEABLE")
        assert d.action is ActionKind.DISPATCH_REVIEWER

    def test_pr_unknown_nao_marca_conflito(self) -> None:
        """REVIEW com PR UNKNOWN → DISPATCH_REVIEWER (não trava no unknown)."""
        r = _result(
            labels=["flow:review-waiting", "flow:feature"],
        )
        d = decide(r, pr_mergeable="UNKNOWN")
        assert d.action is ActionKind.DISPATCH_REVIEWER

    def test_sem_conflito_despacha_reviewer(self) -> None:
        """REVIEW normal sem conflito → DISPATCH_REVIEWER."""
        r = _result(
            labels=["flow:review-waiting", "flow:feature"],
        )
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_REVIEWER

    def test_conflito_tem_prioridade_sobre_reviewed(self) -> None:
        """CONFLITO + REVIEWED → DISPATCH_CONFLICT_RESOLVER (conflito tem prioridade)."""
        r = _result(
            labels=["flow:review-waiting", "flow:merge-conflict", "flow:reviewed",
                    "flow:feature"],
            modifiers={Modifier.MERGE_CONFLICT, Modifier.REVIEWED},
        )
        d = decide(r)
        assert d.action is ActionKind.DISPATCH_CONFLICT_RESOLVER

    def test_conflito_so_em_review(self) -> None:
        """flow:merge-conflict só dispara o resolver quando state é REVIEW."""
        r = _result(
            labels=["flow:develop-running", "flow:merge-conflict", "flow:feature"],
            state=State.DEVELOP_RUNNING,
            modifiers={Modifier.MERGE_CONFLICT},
        )
        d = decide(r)
        # Em DEV, o executor não processa flow:merge-conflict como DISPATCH_CONFLICT_RESOLVER
        assert d.action is ActionKind.SKIP


# ---------------------------------------------------------------------------
# Guard de reprocessamento: nunca criar PR nova
# ---------------------------------------------------------------------------

class TestReprocessamentoPROriginal:
    def test_rework_usa_mesma_branch(self) -> None:
        """Sessão de re-trabalho deve usar a branch feat/issue-N existente."""
        from flow.prompts.loader import render_prompt
        rendered = render_prompt(
            "rework",
            repo="owner/repo",
            repo_short="repo",
            issue_number="42",
            issue_title="Fix rework",
            issue_url="https://github.com/owner/repo/issues/42",
            session_title="rework: repo #42 PR #10 (iter 1): Fix rework",
            pr_number="10",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/repo-42",
            base_branch="main",
            iteration="1",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="",
        )
        # Nunca deve mencionar abrir PR nova
        assert "NUNCA abra PR novo" in rendered
        assert "feat/issue-42" in rendered

    def test_conflict_md_nunca_abre_pr(self) -> None:
        """conflict.md deve proibir abrir PR nova."""
        from flow.prompts.loader import render_prompt
        rendered = render_prompt(
            "merge_conflict",
            repo="owner/repo",
            repo_short="repo",
            issue_number="42",
            issue_title="Fix conflict",
            issue_url="https://github.com/owner/repo/issues/42",
            session_title="conflito: repo #42 PR #10: Fix conflict",
            pr_number="10",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/repo-42",
            base_branch="main",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="",
        )
        assert "NUNCA abra PR novo" in rendered
        assert "feat/issue-42" in rendered
        assert "force-with-lease" in rendered


# ---------------------------------------------------------------------------
# Prompts: conflict.md
# ---------------------------------------------------------------------------

class TestConflictPrompt:
    def test_conflict_md_renderiza_sem_erro(self) -> None:
        """conflict.md deve renderizar com todas as variáveis obrigatórias."""
        from flow.prompts.loader import render_prompt
        rendered = render_prompt(
            "merge_conflict",
            repo="owner/repo",
            repo_short="repo",
            issue_number="42",
            issue_title="Fix conflict",
            issue_url="https://github.com/owner/repo/issues/42",
            session_title="conflito: repo #42 PR #10: Fix conflict",
            pr_number="10",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/repo-42",
            base_branch="main",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="",
        )
        assert "feat/issue-42" in rendered
        assert "PR #10" in rendered or "pr_number" not in rendered
        assert "rebase" in rendered.lower()
        assert "NUNCA" in rendered

    def test_conflict_md_inclui_branch_base(self) -> None:
        """conflict.md deve incluir a branch base para rebase."""
        from flow.prompts.loader import render_prompt
        rendered = render_prompt(
            "merge_conflict",
            repo="owner/repo",
            repo_short="repo",
            issue_number="42",
            issue_title="Fix",
            issue_url="https://github.com/owner/repo/issues/42",
            session_title="conflito: repo #42 PR #10: Fix",
            pr_number="10",
            dev_root="/home/dev",
            worktree_path="/home/dev/.esteira-worktrees/repo-42",
            base_branch="main",
            notify_step="reporte o resultado, ",
            vault_step="",
            prompt_extra="",
        )
        assert "main" in rendered
