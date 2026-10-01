#!/usr/bin/env python3
"""advance_state.py -- Script helper do engine de workflow.

Avança a task para um novo estado no RunLedger E aplica a label no provider.

    python3 advance_state.py "VGAT-123" "flow:review-waiting"

Equivale a: ledger.advance() + provider.set_labels() do _ledger_advance() em deployment.py.
Emite JSON: {"ok": true} ou {"ok": false, "error": "..."}
"""
from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from deployment.deployment import (  # noqa: E402
    _load_config,
    _load_provider_env,
    _resolve_squad_id,
)
from flow.domain.run_ledger import SqliteRunLedger  # noqa: E402
from flow.domain.state import State, transition_state  # noqa: E402
from flow.ports.issue_provider import provider_for  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({"ok": False, "error": "usage: advance_state.py <issue_key> <new_state>"}))
        sys.exit(1)

    issue_key = sys.argv[1].strip()
    new_state_str = sys.argv[2].strip()

    # Valida o estado alvo
    try:
        new_state = State(new_state_str)
    except ValueError:
        print(json.dumps({"ok": False, "error": f"estado inválido: {new_state_str!r}"}))
        sys.exit(1)

    cfg = _load_config()
    squad_id = _resolve_squad_id(cfg)
    issue_provider_name = cfg.get("issue_provider", "github")

    _load_provider_env(issue_provider_name)
    provider = provider_for(issue_provider_name)
    ledger = SqliteRunLedger(squad_id)

    try:
        run = ledger.active()
        if run is None or run.task_key != issue_key:
            print(json.dumps({"ok": False, "error": f"task {issue_key!r} não está ativa no ledger"}))
            sys.exit(1)

        # 1. Avança no ledger (fonte de verdade local)
        ledger.advance(issue_key, new_state.value)

        # 2. Aplica a label no provider (para auditoria/visibilidade)
        try:
            item = provider.get_work_item(run.repo, issue_key)
            current_labels = set(item.get("labels", []))
            new_labels = transition_state(current_labels, new_state)
            provider.set_labels(run.repo, issue_key, new_labels)
        except Exception as exc:
            # Label é auditoria — falha não reverte o ledger
            print(json.dumps({
                "ok": True,
                "warning": f"ledger avançado mas falha ao aplicar label: {exc}",
            }))
            return

        print(json.dumps({"ok": True, "new_state": new_state.value}))

    finally:
        ledger.close()


if __name__ == "__main__":
    main()
