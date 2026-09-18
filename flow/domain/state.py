"""Modelo de estado x modificador do KiroCrew Flow.

Duas dimensões independentes:

    ESTADO     — exatamente 1 por issue, na ordem canônica abaixo
    MODIFICADOR — 0..N, sobrepõem ao estado; os de parada têm prioridade

Regra central: ``is_dispatchable()`` respeita a prioridade de parada.
Uma issue em ``crewflow:todo`` + ``crewflow:blocked`` NÃO é candidata a
dispatch, mesmo estando no estado gatilho.

Todo este módulo é puro Python sem I/O — testável sem mock.
"""

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# Estados (ordem canônica explícita)
# ---------------------------------------------------------------------------

class State(StrEnum):
    """Estados da esteira.  Exatamente um por issue.

    A ordem dos membros É a ordem canônica — ``State.SPEC < State.DONE`` é
    verdadeiro e ``list(State)`` retorna na sequência certa.

    Prefixo ``crewflow:`` é o nome em Jira/GitHub; o nome do membro é o
    nome de código.
    """

    SPEC   = "crewflow:spec"    # PM especificando
    READY  = "crewflow:ready"   # spec pronta, aguardando priorização
    TODO   = "crewflow:todo"    # priorizado — GATILHO da esteira
    DEV    = "crewflow:dev"     # em desenvolvimento
    REVIEW = "crewflow:review"  # PR aberto: review + TL aprova (ANTES do QA)
    QA     = "crewflow:qa"      # deploy HML manual + QA testa (DEPOIS do review)
    DONE   = "crewflow:done"    # concluído

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, State):
            return NotImplemented
        return list(State).index(self) < list(State).index(other)

    def __le__(self, other: object) -> bool:
        if not isinstance(other, State):
            return NotImplemented
        return self == other or self < other  # type: ignore[operator]

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, State):
            return NotImplemented
        return list(State).index(self) > list(State).index(other)

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, State):
            return NotImplemented
        return self == other or self > other  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Modificadores
# ---------------------------------------------------------------------------

class Modifier(StrEnum):
    """Modificadores que se sobrepõem ao estado (0..N por issue).

    Os de parada (``STOP_MODIFIERS``) têm prioridade: mesmo que o estado
    seja o gatilho ``TODO``, a issue não é despachável se um deles estiver
    presente.
    """

    BLOCKED    = "crewflow:blocked"    # para tudo — dependência ou espera humana
    RUNNING    = "crewflow:running"    # trabalho em andamento no estado atual
    REVIEWED   = "crewflow:reviewed"   # lock anti-loop: já analisado neste SHA
    HML_BYPASS = "crewflow:hml-bypass" # exceção auditada: hotfix pulou o HML


# Modificadores que impedem dispatch mesmo com o estado correto.
STOP_MODIFIERS: frozenset[Modifier] = frozenset({
    Modifier.BLOCKED,
    Modifier.RUNNING,
})

# Estado que é o gatilho da esteira
DISPATCH_TRIGGER: State = State.TODO


# ---------------------------------------------------------------------------
# Erros de domínio
# ---------------------------------------------------------------------------

class EstadoAmbiguo(ValueError):
    """Lançado quando uma issue tem mais de um estado simultaneamente.

    Estado é exclusivo: exatamente 1 por issue.
    """

    def __init__(self, estados: set[str]) -> None:
        labels = sorted(estados)
        super().__init__(
            f"issue tem múltiplos estados ao mesmo tempo: {labels!r} — "
            f"remova todos menos um"
        )
        self.estados = estados


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_state(labels: set[str] | frozenset[str]) -> State | None:
    """Extrai o estado único de um conjunto de labels.

    Retorna ``None`` se nenhuma label de estado estiver presente (a issue
    não participa da esteira).

    Lança ``EstadoAmbiguo`` se duas ou mais labels de estado estiverem
    presentes ao mesmo tempo.
    """
    state_values = {s.value for s in State}
    encontrados = {label for label in labels if label in state_values}

    if len(encontrados) == 0:
        return None

    if len(encontrados) > 1:
        raise EstadoAmbiguo(encontrados)

    return State(next(iter(encontrados)))


def parse_modifiers(labels: set[str] | frozenset[str]) -> frozenset[Modifier]:
    """Extrai os modificadores de um conjunto de labels.

    Labels desconhecidas são ignoradas silenciosamente (a issue pode ter
    labels de outros sistemas, ex: ``phase-1``, ``documentation``).
    """
    modifier_values = {m.value: m for m in Modifier}
    return frozenset(
        modifier_values[label]
        for label in labels
        if label in modifier_values
    )


# ---------------------------------------------------------------------------
# Lógica de dispatch
# ---------------------------------------------------------------------------

def is_dispatchable(state: State | None, modifiers: frozenset[Modifier]) -> bool:
    """Decide se a issue é candidata a dispatch.

    Condição necessária e suficiente:
      1. O estado é ``crewflow:todo`` (o gatilho)
      2. Nenhum modificador de parada está presente

    Modificadores de parada: ``crewflow:blocked``, ``crewflow:running``
    """
    if state is None:
        return False
    if state is not DISPATCH_TRIGGER:
        return False
    return not bool(modifiers & STOP_MODIFIERS)


# ---------------------------------------------------------------------------
# Transições válidas
# ---------------------------------------------------------------------------

def can_transition(de: State, para: State) -> bool:
    """Verifica se a transição entre dois estados é válida.

    As transições válidas seguem a ordem canônica com duas exceções:
    - Qualquer estado pode voltar para ``DEV`` (reprovar gate)
    - ``TODO`` pode pular para ``DEV`` (a esteira pegou)

    Transições inválidas: pular mais de um passo à frente (ex: SPEC → DEV),
    ou ir para trás exceto para DEV.
    """
    ordem = list(State)
    idx_de = ordem.index(de)
    idx_para = ordem.index(para)

    # Avançar um passo é sempre válido (exceto de DONE que é terminal)
    if de is State.DONE:
        return False
    if idx_para == idx_de + 1:
        return True

    # Voltar para DEV é sempre válido (reprovar gate)
    return para is State.DEV and de not in (State.SPEC, State.READY, State.TODO, State.DEV)
