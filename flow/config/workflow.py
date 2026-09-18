"""Modelos de workflow e os 4 templates fixos da Fase 1.

Na Fase 1, os workflows são hardcoded como objetos Python.
Na Fase 3, serão carregados de workflows/*.yaml e editáveis no canvas.

Um workflow é um grafo de nós:
  state  — representa um crewflow:* estado
  gate   — decisão com múltiplas saídas (aprova/reprova/troca de template)
  action — ação de automação (dispatch de agente)
  event  — evento do sistema (PR criada, CI passou)

O executor da Fase 1 já implementa esses comportamentos diretamente.
Este módulo formaliza o grafo para que o routing possa resolvê-lo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# ---------------------------------------------------------------------------
# Tipos de nó
# ---------------------------------------------------------------------------

class NodeKind(StrEnum):
    STATE  = "state"    # crewflow:* estado
    GATE   = "gate"     # decisão humana ou automática
    ACTION = "action"   # disparo de agente/automação
    EVENT  = "event"    # evento do sistema


class ActorKind(StrEnum):
    HUMAN      = "human"
    AUTOMATION = "automation"
    SYSTEM     = "system"


class GateAuthority(StrEnum):
    PM     = "pm"
    TL     = "tl"
    QA     = "qa"
    DEV    = "dev"
    SYSTEM = "system"  # pré-condição automática (sem humano)


# ---------------------------------------------------------------------------
# Nó
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WorkflowNode:
    """Um nó do grafo de workflow."""
    id: str                         # ex: "todo", "gate-tl-spec", "kiro-reviewer"
    kind: NodeKind
    label: str                      # descrição legível
    state: str | None = None        # para kind=STATE: o crewflow:* value
    actor: ActorKind = ActorKind.SYSTEM
    authority: GateAuthority | None = None  # para gates humanos
    agent: str | None = None        # para kind=ACTION: ex: "kiro-reviewer"
    skippable: bool = False         # pode ser pulado em templates comprimidos (hotfix)


@dataclass(frozen=True, slots=True)
class WorkflowEdge:
    """Uma aresta do grafo."""
    from_node: str          # id do nó de origem
    to_node: str            # id do nó de destino
    label: str = ""         # descrição (ex: "aprova", "reprova")
    requires_bypass: bool = False   # só permitida com crewflow:hml-bypass


@dataclass(slots=True)
class WorkflowTemplate:
    """Um template de workflow completo (grafo)."""
    id: str
    name: str
    nodes: list[WorkflowNode] = field(default_factory=list)
    edges: list[WorkflowEdge] = field(default_factory=list)

    def get_node(self, node_id: str) -> WorkflowNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def successors(self, node_id: str) -> list[str]:
        """Retorna os ids dos nós que o nó dado pode alcançar."""
        return [e.to_node for e in self.edges if e.from_node == node_id]

    def predecessors(self, node_id: str) -> list[str]:
        return [e.from_node for e in self.edges if e.to_node == node_id]

    def node_ids(self) -> frozenset[str]:
        return frozenset(n.id for n in self.nodes)

    def validate(self) -> list[str]:
        """Retorna lista de erros de validação. Vazia = ok."""
        errors: list[str] = []
        ids = self.node_ids()
        for edge in self.edges:
            if edge.from_node not in ids:
                errors.append(f"aresta de nó inexistente: {edge.from_node!r}")
            if edge.to_node not in ids:
                errors.append(f"aresta para nó inexistente: {edge.to_node!r}")
        # Verifica nós órfãos (sem arestas de entrada e não é o nó inicial)
        if self.nodes:
            for n in self.nodes[1:]:
                if not self.predecessors(n.id):
                    errors.append(f"nó órfão (sem entrada): {n.id!r}")
        return errors


# ---------------------------------------------------------------------------
# Os 4 templates fixos da Fase 1
# ---------------------------------------------------------------------------

def _feature_workflow() -> WorkflowTemplate:
    """Versão C — review ANTES do QA, sequencial."""
    wf = WorkflowTemplate(id="feature-flow", name="Feature (Versão C)")

    nodes = [
        WorkflowNode("spec",     NodeKind.STATE,  "PM especificando",       state="crewflow:spec",   actor=ActorKind.HUMAN),
        WorkflowNode("gate-spec", NodeKind.GATE,  "GATE 1: TL aprova spec", actor=ActorKind.HUMAN,   authority=GateAuthority.TL),
        WorkflowNode("ready",    NodeKind.STATE,  "Aguardando priorização", state="crewflow:ready",  actor=ActorKind.HUMAN),
        WorkflowNode("todo",     NodeKind.STATE,  "Priorizado (gatilho)",   state="crewflow:todo",   actor=ActorKind.AUTOMATION),
        WorkflowNode("dev",      NodeKind.STATE,  "Em desenvolvimento",     state="crewflow:dev",    actor=ActorKind.AUTOMATION),
        WorkflowNode("kiro-reviewer", NodeKind.ACTION, "Code review automático", actor=ActorKind.AUTOMATION, agent="kiro-reviewer"),
        WorkflowNode("gate-review", NodeKind.GATE, "GATE 2: TL aprova review", actor=ActorKind.HUMAN, authority=GateAuthority.TL),
        WorkflowNode("review",   NodeKind.STATE,  "Code review",            state="crewflow:review", actor=ActorKind.HUMAN),
        WorkflowNode("hml",      NodeKind.EVENT,  "Deploy HML manual",      actor=ActorKind.HUMAN),
        WorkflowNode("qa",       NodeKind.STATE,  "QA testa em HML",        state="crewflow:qa",     actor=ActorKind.HUMAN),
        WorkflowNode("gate-qa",  NodeKind.GATE,   "GATE 3: QA aprova",      actor=ActorKind.HUMAN,   authority=GateAuthority.QA),
        WorkflowNode("merge",    NodeKind.EVENT,  "Merge manual",           actor=ActorKind.HUMAN),
        WorkflowNode("done",     NodeKind.STATE,  "Concluído",              state="crewflow:done"),
    ]
    wf.nodes = nodes

    wf.edges = [
        WorkflowEdge("spec",          "gate-spec"),
        WorkflowEdge("gate-spec",     "ready",       "aprova"),
        WorkflowEdge("gate-spec",     "spec",        "reprova"),
        WorkflowEdge("ready",         "todo"),
        WorkflowEdge("todo",          "dev"),
        WorkflowEdge("dev",           "review"),
        WorkflowEdge("review",        "kiro-reviewer"),
        WorkflowEdge("kiro-reviewer", "gate-review"),
        WorkflowEdge("gate-review",   "hml",         "aprova"),
        WorkflowEdge("gate-review",   "dev",         "reprova"),
        WorkflowEdge("hml",           "qa"),
        WorkflowEdge("qa",            "gate-qa"),
        WorkflowEdge("gate-qa",       "merge",       "aprova"),
        WorkflowEdge("gate-qa",       "dev",         "reprova"),
        WorkflowEdge("merge",         "done"),
    ]
    return wf


def _hotfix_workflow() -> WorkflowTemplate:
    """Hotfix — Versão C comprimida, bypass auditado do HML."""
    wf = WorkflowTemplate(id="hotfix-flow", name="Hotfix / Incidente PRD")

    nodes = [
        WorkflowNode("gate-0",      NodeKind.GATE,   "GATE 0: triagem (é PRD?)",   actor=ActorKind.SYSTEM, authority=GateAuthority.SYSTEM),
        WorkflowNode("todo",        NodeKind.STATE,  "Priorizado",                 state="crewflow:todo",   actor=ActorKind.AUTOMATION,  skippable=True),
        WorkflowNode("dev",         NodeKind.STATE,  "Implementa fix",             state="crewflow:dev",    actor=ActorKind.AUTOMATION),
        WorkflowNode("kiro-reviewer", NodeKind.ACTION, "Code review (inviolável)", actor=ActorKind.AUTOMATION, agent="kiro-reviewer"),
        WorkflowNode("gate-review", NodeKind.GATE,   "GATE 1: TL aprova (inviolável)", actor=ActorKind.HUMAN, authority=GateAuthority.TL),
        WorkflowNode("review",      NodeKind.STATE,  "Review",                     state="crewflow:review", actor=ActorKind.HUMAN),
        WorkflowNode("hml",         NodeKind.EVENT,  "Deploy HML manual",          actor=ActorKind.HUMAN,   skippable=True),
        WorkflowNode("hml-bypass",  NodeKind.EVENT,  "Bypass HML (auditado)",      actor=ActorKind.HUMAN),
        WorkflowNode("qa",          NodeKind.STATE,  "Smoke test",                 state="crewflow:qa",     actor=ActorKind.HUMAN),
        WorkflowNode("gate-qa",     NodeKind.GATE,   "GATE 2: incidente parou?",   actor=ActorKind.HUMAN,   authority=GateAuthority.QA),
        WorkflowNode("merge",       NodeKind.EVENT,  "Merge manual",               actor=ActorKind.HUMAN),
        WorkflowNode("done",        NodeKind.STATE,  "PRD corrigida",              state="crewflow:done"),
        WorkflowNode("postmortem",  NodeKind.EVENT,  "Post-mortem (obrigatório)",  actor=ActorKind.HUMAN),
    ]
    wf.nodes = nodes

    wf.edges = [
        WorkflowEdge("gate-0",      "todo",         "é PRD"),
        WorkflowEdge("gate-0",      "done",         "não é PRD — rebaixar pra bug"),
        WorkflowEdge("todo",        "dev"),
        WorkflowEdge("dev",         "review"),
        WorkflowEdge("review",      "kiro-reviewer"),
        WorkflowEdge("kiro-reviewer", "gate-review"),
        WorkflowEdge("gate-review", "hml",          "aprova — caminho normal"),
        WorkflowEdge("gate-review", "hml-bypass",   "aprova — bypass auditado", requires_bypass=True),
        WorkflowEdge("gate-review", "dev",          "reprova"),
        WorkflowEdge("hml",         "qa"),
        WorkflowEdge("hml-bypass",  "qa"),
        WorkflowEdge("qa",          "gate-qa"),
        WorkflowEdge("gate-qa",     "merge",        "parou"),
        WorkflowEdge("gate-qa",     "dev",          "não parou"),
        WorkflowEdge("merge",       "done"),
        WorkflowEdge("done",        "postmortem"),
    ]
    return wf


def _bug_workflow() -> WorkflowTemplate:
    """Bug — mesma ordem da Versão C com investigação shift-left."""
    wf = _feature_workflow()
    wf.id = "bug-flow"
    wf.name = "Bug"
    # Em Fase 2: adicionar nó de investigação shift-left na entrada.
    # Por ora é idêntico ao feature — o executor distingue pelo template.
    return wf


def _debt_workflow() -> WorkflowTemplate:
    """Débito técnico — autoridade TL na entrada, pré-condição COV."""
    wf = WorkflowTemplate(id="debt-flow", name="Débito Técnico")

    nodes = [
        WorkflowNode("gate-tl",    NodeKind.GATE,  "GATE DT: TL aprova (técnico)", actor=ActorKind.HUMAN, authority=GateAuthority.TL),
        WorkflowNode("todo",       NodeKind.STATE, "Priorizado",                    state="crewflow:todo",  actor=ActorKind.AUTOMATION),
        WorkflowNode("dev",        NodeKind.STATE, "Implementa + refatora",         state="crewflow:dev",   actor=ActorKind.AUTOMATION),
        WorkflowNode("cov",        NodeKind.GATE,  "PRÉ-COND COV: teste de equiv.", actor=ActorKind.SYSTEM, authority=GateAuthority.SYSTEM),
        WorkflowNode("kiro-reviewer", NodeKind.ACTION, "Code review",               actor=ActorKind.AUTOMATION, agent="kiro-reviewer"),
        WorkflowNode("gate-review", NodeKind.GATE, "GATE 1: TL aprova review",      actor=ActorKind.HUMAN,  authority=GateAuthority.TL),
        WorkflowNode("review",     NodeKind.STATE, "Review",                        state="crewflow:review", actor=ActorKind.HUMAN),
        WorkflowNode("hml",        NodeKind.EVENT, "Deploy HML manual",             actor=ActorKind.HUMAN),
        WorkflowNode("qa",         NodeKind.STATE, "Regressão (nada mudou?)",       state="crewflow:qa",    actor=ActorKind.HUMAN),
        WorkflowNode("gate-qa",    NodeKind.GATE,  "GATE 2: equivalência ok?",      actor=ActorKind.HUMAN,  authority=GateAuthority.QA),
        WorkflowNode("merge",      NodeKind.EVENT, "Merge manual",                  actor=ActorKind.HUMAN),
        WorkflowNode("done",       NodeKind.STATE, "Concluído",                     state="crewflow:done"),
    ]
    wf.nodes = nodes

    wf.edges = [
        WorkflowEdge("gate-tl",    "todo",         "aprova"),
        WorkflowEdge("gate-tl",    "gate-tl",      "reprova — aguarda revisão"),  # self-loop intencional
        WorkflowEdge("todo",       "dev"),
        WorkflowEdge("dev",        "cov"),
        WorkflowEdge("cov",        "review",       "teste existe"),
        WorkflowEdge("cov",        "dev",          "sem teste — escreva primeiro"),
        WorkflowEdge("review",     "kiro-reviewer"),
        WorkflowEdge("kiro-reviewer", "gate-review"),
        WorkflowEdge("gate-review", "hml",         "aprova"),
        WorkflowEdge("gate-review", "dev",         "reprova"),
        WorkflowEdge("hml",        "qa"),
        WorkflowEdge("qa",         "gate-qa"),
        WorkflowEdge("gate-qa",    "merge",        "equivalente"),
        WorkflowEdge("gate-qa",    "dev",          "comportamento mudou"),
        WorkflowEdge("merge",      "done"),
    ]
    return wf


# ---------------------------------------------------------------------------
# Registro de templates
# ---------------------------------------------------------------------------

_cache: dict[str, dict[str, WorkflowTemplate] | None] = {"registry": None}


def get_template(name: str) -> WorkflowTemplate | None:
    """Retorna um template pelo nome, ou None se não encontrado."""
    if _cache["registry"] is None:
        _cache["registry"] = {wf.id: wf for wf in [
            _feature_workflow(),
            _bug_workflow(),
            _hotfix_workflow(),
            _debt_workflow(),
        ]}
    registry: dict[str, WorkflowTemplate] = _cache["registry"]  # type: ignore[assignment]
    return registry.get(name)


def list_templates() -> list[str]:
    """Retorna os ids dos templates disponíveis."""
    get_template("feature-flow")  # inicializa o cache
    assert _cache["registry"] is not None
    registry: dict[str, WorkflowTemplate] = _cache["registry"]  # type: ignore[assignment]
    return sorted(registry)
