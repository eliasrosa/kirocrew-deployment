#!/usr/bin/env python3
"""merge_pr.py -- Script helper do engine de workflow.

Faz o merge squash da PR associada à issue e avança o ledger para flow:done.

    python3 merge_pr.py "VGAT-123"

Retorna exit_code 0 em sucesso, 1 em falha.
Emite JSON: {"ok": true, "pr_number": N} | {"ok": false, "error": "..."}

Equivale a: _execute_auto_merges() + advance_state(flow:done) do deployment.py.
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
    _task_number_from_key,
)
from flow.domain.run_ledger import RunStatus, SqliteRunLedger  # noqa: E402
from flow.domain.state import State, transition_state  # noqa: E402
from flow.ports.issue_provider import provider_for  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "usage: merge_pr.py <issue_key>"}))
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
            print(json.dumps({"ok": False, "error": f"task {issue_key!r} não está ativa no ledger"}))
            sys.exit(1)

        issue_number = _task_number_from_key(issue_key)
        if issue_number is None:
            print(json.dumps({"ok": False, "error": f"não foi possível extrair número da issue de {issue_key!r}"}))
            sys.exit(1)

        # Localiza e faz o merge da PR via github_client
        import contextlib

        from flow.adapters import github_client as gh_client

        pr = gh_client.get_pr_for_issue(run.repo, issue_number)
        if pr is None:
            print(json.dumps({"ok": False, "error": f"PR aberta para #{issue_number} não encontrada em {run.repo}"}))
            sys.exit(1)

        pr_number = pr["number"]
        pr_branch = pr.get("headRefName") or pr.get("head", {}).get("ref", "")

        # Merge squash
        gh_client.merge_pull_request(run.repo, pr_number, merge_method="squash")

        # Deleta a branch após merge (fail-safe)
        if pr_branch:
            with contextlib.suppress(Exception):
                gh_client.delete_branch(run.repo, pr_branch)

        # Avança ledger para done e libera o slot
        ledger.advance(issue_key, State.DONE.value)
        ledger.release(issue_key, RunStatus.DONE)

        # Aplica label flow:done no provider (auditoria)
        with contextlib.suppress(Exception):
            item = provider.get_work_item(run.repo, issue_key)
            current_labels = set(item.get("labels", []))
            new_labels = transition_state(current_labels, State.DONE)
            provider.set_labels(run.repo, issue_key, new_labels)

        print(json.dumps({"ok": True, "pr_number": pr_number, "branch_deleted": bool(pr_branch)}))

    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)

    finally:
        ledger.close()


if __name__ == "__main__":
    main()
