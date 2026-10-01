#!/usr/bin/env python3
"""check_pr_state.py -- Script helper do engine de workflow.

Lê o estado atual da PR da issue (via evidências externas: branch/PR/reviews).

    python3 check_pr_state.py "VGAT-123"
    → {"state": "review_approved"} | {"state": "review_refused"} | {"state": "pending"}

Equivale a: _LedgerStateReader.read_state() focado no resultado de review.
Custo: 1 chamada de rede ao provider (gh CLI ou Jira adapter).
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
    _LedgerStateReader,
    _load_config,
    _load_provider_env,
    _resolve_squad_id,
)
from flow.domain.run_ledger import SqliteRunLedger  # noqa: E402
from flow.domain.state import State  # noqa: E402
from flow.ports.issue_provider import provider_for  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"state": "pending", "error": "usage: check_pr_state.py <issue_key>"}))
        sys.exit(1)

    issue_key = sys.argv[1].strip()

    cfg = _load_config()
    squad_id = _resolve_squad_id(cfg)
    issue_provider_name = cfg.get("issue_provider", "github")

    _load_provider_env(issue_provider_name)
    provider = provider_for(issue_provider_name)
    ledger = SqliteRunLedger(squad_id)

    try:
        run = ledger.active()
        if run is None or run.task_key != issue_key:
            print(json.dumps({"state": "pending", "reason": "task not active in ledger"}))
            return

        reader = _LedgerStateReader(provider)
        real = reader.read_state(run.repo, issue_key)

        if real is None:
            print(json.dumps({"state": "pending"}))
            return

        # Mapeia State → nome para o engine YAML
        if real in (State.REVIEW_APPROVED, State.QA_WAITING, State.QA_APPROVED, State.DONE):
            state_name = "review_approved"
        elif real == State.REVIEW_REFUSED:
            state_name = "review_refused"
        else:
            state_name = "pending"

        print(json.dumps({"state": state_name, "raw_state": real.value}))

    finally:
        ledger.close()


if __name__ == "__main__":
    main()
