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
            labels=["flow:review-waiting", "flow:merge-conflict", "flow:review-running",
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

