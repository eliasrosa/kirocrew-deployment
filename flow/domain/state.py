"""Modelo de estado x modificador do KiroCrew Flow.

Duas dimensões independentes:

    ESTADO     — exatamente 1 por issue, na ordem canônica abaixo
    MODIFICADOR — 0..N, sobrepõem ao estado; os de parada têm prioridade

Regra central: ``is_dispatchable()`` respeita a prioridade de parada.
Uma issue em ``flow:develop-waiting`` + ``flow:blocked`` NÃO é candidata a
dispatch, mesmo estando no estado gatilho.

Todo este módulo é puro Python sem I/O — testável sem mock.

Migração crewflow:* → flow:*
-----------------------------
| crewflow           | flow                   |
|--------------------|------------------------|
| crewflow:spec      | flow:briefing          |
| crewflow:ready     | flow:planning-specs    |
| crewflow:todo      | flow:develop-waiting   |
| crewflow:dev       | flow:develop-running   |
| crewflow:review    | flow:review-waiting    |
| crewflow:review-ok | flow:review-approved   |
| crewflow:review-fail | flow:review-refused  |
| crewflow:qa        | flow:qa-waiting        |
| crewflow:done      | flow:done              |
| crewflow:blocked   | flow:blocked           |
| crewflow:conflito  | flow:merge-conflict    |
Novos: flow:planning-review, flow:qa-approved, flow:qa-refused,
       flow:develop-waiting, flow:develop-running
Removidos: crewflow:reviewed (lock vira interno), crewflow:changes-requested,
           crewflow:running (modificador absorvido em develop-running),
           crewflow:hml-bypass, labels de tipo e prioridade.
"""

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# Estados (ordem canônica explícita)
# ---------------------------------------------------------------------------

class State(StrEnum):
    """Estados da esteira.  Exatamente um por issue.

    A ordem dos membros É a ordem canônica — ``State.BRIEFING < State.DONE``
    é verdadeiro e ``list(State)`` retorna na sequência certa.

    Prefixo ``flow:`` é o nome em Jira/GitHub; o nome do membro é o
    nome de código.
    """

    BRIEFING         = "flow:briefing"          # TL/PM criou demanda + briefing
    PLANNING_SPECS   = "flow:planning-specs"    # Dev montando spec/critérios/sub-tasks
    PLANNING_REVIEW  = "flow:planning-review"   # Dev pediu revisão ao TL/PM
    DEVELOP_WAITING  = "flow:develop-waiting"   # Aguardando agente pegar — GATILHO
    DEVELOP_RUNNING  = "flow:develop-running"   # Agente implementando
    REVIEW_WAITING   = "flow:review-waiting"    # PR aberta, aguardando reviewer
    REVIEW_APPROVED  = "flow:review-approved"   # Reviewer aprovou
    REVIEW_REFUSED   = "flow:review-refused"    # Reviewer reprovou — gate humano
    QA_WAITING       = "flow:qa-waiting"        # Aguardando QA
    QA_TESTING       = "flow:qa-testing"        # QA testando
    QA_APPROVED      = "flow:qa-approved"       # QA aprovou — gatilho merge
    QA_REFUSED       = "flow:qa-refused"        # QA reprovou — gate humano
    DONE             = "flow:done"              # Concluído

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
    seja o gatilho ``DEVELOP_WAITING``, a issue não é despachável se um
    deles estiver presente.
    """

    BLOCKED        = "flow:blocked"         # para tudo — dependência ou espera humana
    MERGE_CONFLICT = "flow:merge-conflict"  # PR tem conflito de merge ou base desatualizada

    # Modificador interno de lock anti-loop (não exposto como label de negócio)
    # Mantido como enum para uso interno pelo executor/deployment.
    REVIEWED       = "flow:reviewed"        # lock anti-loop interno: já analisado neste SHA


# Modificadores que impedem dispatch mesmo com o estado correto.
STOP_MODIFIERS: frozenset[Modifier] = frozenset({
    Modifier.BLOCKED,
})

# Estado que é o gatilho da esteira
DISPATCH_TRIGGER: State = State.DEVELOP_WAITING


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
      1. O estado é ``flow:develop-waiting`` (o gatilho)
      2. Nenhum modificador de parada está presente

    Modificadores de parada: ``flow:blocked``
    """
    if state is None:
        return False
    if state is not DISPATCH_TRIGGER:
        return False
    return not bool(modifiers & STOP_MODIFIERS)


# ---------------------------------------------------------------------------
# Transição atômica de estado
# ---------------------------------------------------------------------------

#: Conjunto de todos os valores de estado para filtragem rápida.
_STATE_VALUES: frozenset[str] = frozenset(s.value for s in State)


def transition_state(
    labels: set[str] | frozenset[str],
    new_state: State,
) -> frozenset[str]:
    """Aplica uma transição de estado de forma atômica.

    Garante exclusividade mútua: retorna o conjunto de labels com
    **exatamente 1** estado (``new_state``), removendo todos os outros
    estados anteriores.  Modificadores (``blocked``, ``merge-conflict``,
    …) e labels de tipo/prioridade/outros sistemas são preservados intactos.

    Esta é a fonte única de verdade para qualquer troca de estado — use
    esta função sempre que a esteira precisar aplicar um novo estado, seja
    no driving adapter (``deployment.py``), nos helpers de teste ou nos
    adapters.  Nunca faça add + remove manual de estado diretamente.

    Args:
        labels:    Conjunto atual de labels da issue.
        new_state: Estado de destino.

    Returns:
        Novo conjunto de labels com exatamente 1 estado (``new_state``)
        e todos os modificadores/labels externos preservados.

    Example::

        labels = {"flow:develop-waiting", "flow:blocked", "phase-1"}
        result = transition_state(labels, State.DEVELOP_RUNNING)
        # → frozenset({"flow:develop-running", "flow:blocked", "phase-1"})
        # flow:develop-waiting foi removido; flow:develop-running foi adicionado.
    """
    # Remove TODOS os estados anteriores, adiciona o novo
    without_states = frozenset(lbl for lbl in labels if lbl not in _STATE_VALUES)
    return without_states | frozenset({new_state.value})


# ---------------------------------------------------------------------------
# Transições válidas
# ---------------------------------------------------------------------------

def can_transition(de: State, para: State) -> bool:
    """Verifica se a transição entre dois estados é válida.

    As transições válidas seguem a ordem canônica com exceções:
    - Qualquer estado pós-DEVELOP_RUNNING pode voltar para DEVELOP_WAITING
      (reprovação de gate human: review-refused, qa-refused)
    - DEVELOP_WAITING pode ir diretamente para DEVELOP_RUNNING (esteira pega)

    Transições inválidas: pular mais de um passo à frente (ex: BRIEFING → DEVELOP_RUNNING).
    """
    ordem = list(State)
    idx_de = ordem.index(de)
    idx_para = ordem.index(para)

    # DONE é terminal
    if de is State.DONE:
        return False

    # Avançar um passo é sempre válido
    if idx_para == idx_de + 1:
        return True

    # Voltar para DEVELOP_WAITING é válido após gates humanos (review-refused, qa-refused)
    return para is State.DEVELOP_WAITING and de not in (
        State.BRIEFING, State.PLANNING_SPECS, State.PLANNING_REVIEW, State.DEVELOP_WAITING, State.DEVELOP_RUNNING
    )
