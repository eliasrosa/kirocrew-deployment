"""Regras dos gates do KiroCrew Flow.

Os gates são regras de negócio puras — sem I/O, sem chamada de rede, sem disco.
Cada gate recebe valores de domínio e devolve um ``Result``.

A regra mais importante do fluxo inteiro vive aqui:

    can_leave_spec(item, squad) -> Result

Para sair de ``crewflow:spec``, o trabalho tem que estar decomposto de forma que
**cada unidade despachável tenha exatamente UM repo**. Mencionar dois repos numa
issue não basta — ela fica indespachável porque o motor não tem como escolher.

Os 4 fluxos (feature, bug, hotfix, débito técnico) expuseram 5 tipos de gate:

1. Gate de triagem — pode TROCAR o template (ex: GATE 0 do hotfix rebaixa pra bug)
2. Gate com autoridade variável — PM no feature, TL no débito técnico
3. Gate de aprovação humana inviolável (GATE 1)
4. Gate de QA — prova que nada mudou (débito) ou que incidente parou (hotfix)
5. Pré-condição de nó — sem aprovação humana; devolve pro dev se não cumprida

Todo este módulo é puro Python sem I/O — testável sem mock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Result — envelope de retorno
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Result:
    """Envelope de retorno dos gates.

    ``ok`` é True quando a condição foi satisfeita.
    ``reason`` explica o problema quando ``ok`` é False — vai pro comentário de
    auditoria da issue (#8), então deve ser legível por humano.
    ``data`` carrega o valor resolvido quando relevante (ex: o nome do repo).
    """

    ok: bool
    reason: str = ""
    data: object = None

    @classmethod
    def success(cls, data: object = None) -> Result:
        return cls(ok=True, data=data)

    @classmethod
    def fail(cls, reason: str) -> Result:
        return cls(ok=False, reason=reason)

    @property
    def failed(self) -> bool:
        return not self.ok

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------------------
# WorkItem e Squad — tipos de entrada (injetados, nunca buscados aqui)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkItem:
    """Representação mínima de um item de trabalho.

    Vem da normalização do adapter (GitHub ou Jira), nunca de I/O direto aqui.
    """

    key: str           # ex: "VGAT-123" ou "org/repo#42"
    title: str         # ex: "[api-gateway2] Ajustar validação de split"
    labels: frozenset[str] = field(default_factory=frozenset)
    parent_key: str | None = None   # para subtarefa/sub-bug


@dataclass(frozen=True, slots=True)
class Squad:
    """Configuração da squad — vem do arquivo squads/*.yaml.

    ``repos`` é a lista autoritativa de repos conhecidos.
    Só nomes curtos (ex: "api-gateway2"), sem "org/".
    """

    id: str
    repos: frozenset[str]


# ---------------------------------------------------------------------------
# Parsing do título — o que NÃO é "primeiro colchete"
# ---------------------------------------------------------------------------

# Prefixos que devem ser ignorados ao buscar o repo no título.
# Spike usa [spike][repo], AIDEN usa [voomp][repo][env].
_IGNORED_PREFIXES = frozenset({
    "spike", "voomp", "str", "hml", "prd", "dev",
    # adicionar outros prefixos do time conforme necessário
})

_BRACKET_PATTERN = re.compile(r"\[([^\]]+)\]")


def parse_repos_from_title(title: str, known: frozenset[str]) -> list[str]:
    """Extrai os repos declarados no título, casando contra a lista da squad.

    NÃO é "primeiro colchete" — o título pode ter prefixos antes do repo:
      [spike][api-gateway2] Investigar...   → ["api-gateway2"]
      [voomp][api-gateway2][str] Teste...   → ["api-gateway2"]
      [api-gateway2] Fix normal...          → ["api-gateway2"]
      [api-gateway2][api-subscription2] ... → ["api-gateway2", "api-subscription2"]
      Sem colchete algum                    → []

    Typo falha alto: [api-gatewy2] não casa com nada → retorna [].
    """
    tokens = [m.group(1).strip().lower() for m in _BRACKET_PATTERN.finditer(title)]
    return [t for t in tokens if t in known and t not in _IGNORED_PREFIXES]


# ---------------------------------------------------------------------------
# Tipos de gate
# ---------------------------------------------------------------------------

class GateAuthority(Enum):
    """Quem tem autoridade sobre o gate."""
    PM  = "pm"     # product manager — aprova o "o quê"
    TL  = "tl"     # tech lead — aprova a justificativa técnica
    QA  = "qa"     # QA / testador
    DEV = "dev"    # desenvolvedor (double-check próprio)


class TemplateSwitch(Enum):
    """Para onde um gate de triagem pode desviar."""
    BUG     = "bug"
    FEATURE = "feature"


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """Resultado de um gate de triagem que pode trocar o template.

    Se ``switch`` não é None, o motor deve usar o novo template em vez do atual.
    """
    result: Result
    switch: TemplateSwitch | None = None


# ---------------------------------------------------------------------------
# GATE 1 — a garantia do spec
# ---------------------------------------------------------------------------

def can_leave_spec(item: WorkItem, squad: Squad) -> Result:
    """GATE 1 — o trabalho pode sair de crewflow:spec?

    Condição: cada unidade despachável tem que ter EXATAMENTE um repo
    declarado no título, casando contra a lista da squad.

    Subtarefa e Sub-bug herdam do pai — não passam por este gate.
    Se ``item.parent_key`` está preenchido, retorna sucesso e delega.
    """
    # Subtarefa/sub-bug herdam do pai — não são unidades independentes
    if item.parent_key is not None:
        return Result.success(data="inherited_from_parent")

    repos = parse_repos_from_title(item.title, squad.repos)

    if not repos:
        return Result.fail(
            f"título sem [repo] reconhecido — declare o repo da squad. "
            f"Repos conhecidos: {sorted(squad.repos)!r}. "
            f"Título: {item.title!r}"
        )

    if len(repos) > 1:
        return Result.fail(
            f"múltiplos repos numa issue: {repos!r} — decomponha em uma "
            f"issue por repo. Título: {item.title!r}"
        )

    return Result.success(data=repos[0])


# ---------------------------------------------------------------------------
# GATE 0 (hotfix) — triagem: é mesmo hotfix ou rebaixa pra bug?
# ---------------------------------------------------------------------------

def triage_hotfix(item: WorkItem) -> GateVerdict:
    """GATE 0 do hotfix — este item é realmente um incidente de PRD?

    Um item com ``crewflow:hotfix`` que NÃO tem ``crewflow:p1`` pode não ser
    urgência real. O gate retorna um ``switch=BUG`` para o motor rebaixar.

    Esta é uma heurística, não uma regra absoluta — o TL que aprova o GATE 1
    pode sobrescrever a triagem.
    """

    has_p1 = "crewflow:p1" in item.labels

    if not has_p1:
        return GateVerdict(
            result=Result.fail(
                "hotfix sem crewflow:p1 — confirme se é incidente de PRD. "
                "Se não for, rebaixe para crewflow:bug."
            ),
            switch=TemplateSwitch.BUG,
        )

    return GateVerdict(result=Result.success())


# ---------------------------------------------------------------------------
# GATE de entrada do débito técnico — autoridade TL
# ---------------------------------------------------------------------------

def can_start_debt(item: WorkItem, has_tl_approval: bool) -> Result:
    """GATE de entrada do débito técnico — TL, não PM.

    No débito técnico a justificativa é técnica: o TL aprova, e não o PM.
    Esta separação é o que o modelo hexagonal permite testar sem infra:
    ``has_tl_approval`` é injetado pelo executor, nunca buscado aqui.
    """
    if not has_tl_approval:
        return Result.fail(
            "débito técnico requer aprovação do TL (justificativa técnica, "
            "não de produto). Aguardando aprovação."
        )
    return Result.success()


# ---------------------------------------------------------------------------
# Pré-condição COV — equivalência de comportamento (débito técnico)
# ---------------------------------------------------------------------------

def has_equivalence_test(item: WorkItem, test_exists: bool) -> Result:
    """Pré-condição COV — existe teste que prova equivalência de comportamento?

    NÃO é um gate de aprovação humana. É uma pré-condição que devolve pro dev
    se não cumprida, sem intervenção humana.

    Refatorar sem teste que prove equivalência é o jeito mais comum de
    transformar dívida técnica em incidente.

    ``test_exists`` é injetado pelo executor (que verifica o CI ou examina
    o PR) — nunca buscado aqui.
    """
    if not test_exists:
        return Result.fail(
            "não há teste que prove equivalência de comportamento. "
            "Escreva o teste contra o CÓDIGO ANTIGO (deve passar antes "
            "e depois da refatoração) antes de continuar."
        )
    return Result.success()


# ---------------------------------------------------------------------------
# Teto de iterações review↔dev (anti-loop infinito)
# ---------------------------------------------------------------------------

DEFAULT_MAX_REVIEW_ITERATIONS = 3


def exceeded_review_iterations(
    item: WorkItem,
    iterations: int,
    max_iterations: int = DEFAULT_MAX_REVIEW_ITERATIONS,
) -> Result:
    """Verifica se o ciclo review↔dev atingiu o teto configurado.

    Evita loop infinito quando dev e reviewer nunca chegam a acordo.
    Quando o teto é atingido, o motor escala para NOTIFY_HUMAN tl em vez de
    continuar despachando sessões dev automaticamente.

    ``iterations`` é o número de iterações completas já realizadas (cada
    round de re-trabalho conta como +1). Injetado pelo executor a partir do
    ``StateComment.review_iterations`` — nunca buscado aqui.
    """
    if iterations >= max_iterations:
        return Result.fail(
            f"teto de iterações review↔dev atingido ({iterations}/{max_iterations}). "
            f"Escalando para TL — revise se o ciclo está progredindo ou se há "
            f"um conflito de requisitos não resolvido."
        )
    return Result.success()


# ---------------------------------------------------------------------------
# Validação do bypass do HML (hotfix)
# ---------------------------------------------------------------------------

def validate_hml_bypass(item: WorkItem, justification: str | None) -> Result:
    """Valida a exceção auditada de bypass do HML.

    O motor bloqueia o merge quando ``crewflow:hml-bypass`` está presente e
    não há justificativa registrada no comentário da issue.

    ``justification`` é o texto extraído do comentário estruturado pelo
    executor (#8) — nunca buscado aqui.
    """
    from flow.domain.state import Modifier

    has_bypass = Modifier.HML_BYPASS.value in item.labels

    if not has_bypass:
        return Result.success()  # não é bypass, nada a validar

    if not justification or not justification.strip():
        return Result.fail(
            "crewflow:hml-bypass presente mas justificativa ausente. "
            "Registre o motivo no comentário da issue antes do merge. "
            "O motor não permite merge sem justificativa."
        )

    return Result.success(data=justification.strip())
