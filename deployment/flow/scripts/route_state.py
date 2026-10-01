#!/usr/bin/env python3
"""route_state.py -- Script helper do engine de workflow.

Recebe um estado flow:* como argumento e emite o nome do próximo nó YAML:

    python3 route_state.py "flow:briefing"
    → {"next_node": "briefing"}

Puro Python, zero I/O de rede. Só mapeia o estado para o nó correto.
Equivale ao match de _ACTIVE_STAGES / estados de espera do ledger_tick.py.
"""
from __future__ import annotations

import json
import sys

# Mapeamento state → nó YAML (deve estar sincronizado com voomp-dev-flow.yaml)
_STATE_TO_NODE: dict[str, str] = {
    "flow:briefing":        "briefing",
    "flow:planning-specs":  "planning_specs",
    "flow:planning-review": "planning_review",
    "flow:develop-waiting": "develop_waiting",
    "flow:develop-running": "develop_waiting",  # running = mesma espera (sessão ativa)
    "flow:review-waiting":  "review_waiting",
    "flow:review-approved": "qa_waiting",        # approved → avança direto para qa
    "flow:review-refused":  "review_waiting",    # refused → permanece em wait (gate já tratou)
    "flow:qa-waiting":      "qa_waiting",
    "flow:qa-testing":      "qa_waiting",
    "flow:qa-approved":     "done",
    "flow:qa-refused":      "qa_waiting",        # refused → permanece em wait (gate já tratou)
    "flow:done":            "done",
}


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"next_node": None, "error": "state argument missing"}))
        sys.exit(1)

    state = sys.argv[1].strip()
    node = _STATE_TO_NODE.get(state)

    if node is None:
        print(json.dumps({"next_node": None, "error": f"unknown state: {state!r}"}))
        sys.exit(1)

    print(json.dumps({"next_node": node}))


if __name__ == "__main__":
    main()
