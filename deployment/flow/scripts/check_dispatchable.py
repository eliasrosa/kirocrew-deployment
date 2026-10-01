#!/usr/bin/env python3
"""check_dispatchable.py -- Script helper do engine de workflow.

Lê a task ativa do RunLedger + estado real da issue (1 query + 1 chamada de rede).
Emite JSON para stdout:

    {
        "dispatchable": bool,   # true = há task pronta para avançar
        "state": "flow:...",    # estado atual da task no ledger
        "issue_key": "...",     # ex: "VGAT-123" ou "owner/repo#42"
        "repo": "...",          # repo resolvido
        "blocked": bool,        # Modifier.BLOCKED presente
        "conflict": bool        # Modifier.MERGE_CONFLICT presente
    }

Equivale a: ledger.active() + reader.read_state() do ledger_tick.py.
Custo: O(1) — 1 query SQLite + 1 chamada de rede ao provider.
"""
from __future__ import annotations

import json
import os
import sys

# Garante que o pacote raiz do kirocrew-flow está no path
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from deployment.deployment import (  # noqa: E402
    _LedgerStateReader,
    _load_config,
    _load_provider_env,
    _resolve_squad_id,
)
from flow.domain.run_ledger import SqliteRunLedger  # noqa: E402
from flow.domain.state import Modifier, State, parse_modifiers  # noqa: E402
from flow.ports.issue_provider import provider_for  # noqa: E402


def main() -> None:
    cfg = _load_config()
    squad_id = _resolve_squad_id(cfg)
    issue_provider_name = cfg.get("issue_provider", "github")

    _load_provider_env(issue_provider_name)
    provider = provider_for(issue_provider_name)
    ledger = SqliteRunLedger(squad_id)
    try:
        run = ledger.active()
        if run is None:
            print(json.dumps({
                "dispatchable": False,
                "state": None,
                "issue_key": None,
                "repo": None,
                "blocked": False,
                "conflict": False,
            }))
            return

        # Detecta modificadores a partir da task (ex: labels Jira/GitHub)
        blocked = False
        conflict = False
        try:
            item = provider.get_work_item(run.repo, run.task_key)
            labels = frozenset(item.get("labels", []))
            mods = parse_modifiers(labels)
            blocked = Modifier.BLOCKED in mods
            conflict = Modifier.MERGE_CONFLICT in mods
        except Exception:
            pass  # fail-safe: incerteza não bloqueia

        # Estado real da issue (evidência externa: branch/PR/issue fechada)
        reader = _LedgerStateReader(provider)
        real_state = reader.read_state(run.repo, run.task_key)

        # Usa estado real se disponível, senão o do ledger
        state = (real_state or State(run.current_stage)).value if real_state else run.current_stage

        dispatchable = (
            not blocked
            and not conflict
            and run.current_stage not in (State.DONE.value,)
        )

        print(json.dumps({
            "dispatchable": dispatchable,
            "state": state,
            "issue_key": run.task_key,
            "repo": run.repo,
            "blocked": blocked,
            "conflict": conflict,
        }))
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
