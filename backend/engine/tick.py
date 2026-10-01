"""
backend/engine/tick.py
Lógica do tick do workflow engine.

Processa todos os runs ativos (ou um específico) a cada chamada.
Síncrono — filesystem + SQLite; rodar em asyncio.to_thread no loop principal.

Gaps de alta prioridade implementados:
  1. Nós terminais explícitos (end / fail) -- workflow_runs.status = completed | failed
  2. Diferenciação de erros (failed vs timed_out vs error) -- error_kind em node_states
  3. Aprovação humana (action/gate) -- workflow_gate_tokens + polling sem sessão de chat
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.engine import db
from backend.engine.loader import WorkflowDefinition, load as load_workflow

# Dir de sessões do Kiro Crew
SESSIONS_DIR = Path.home() / ".kiro/crew/sessions"

# Regex para extrair WORKFLOW_EXIT da última mensagem do agente
EXIT_PATTERN = re.compile(r"WORKFLOW_EXIT:\s*(\{.*?\})", re.DOTALL)


# ── Entry point ───────────────────────────────────────────────────────────

def run_tick(
    workflow_path: str | Path,
    run_id: str | None = None,
) -> None:
    """Processa todos os runs ativos do workflow (ou um específico).

    Args:
        workflow_path: caminho para o arquivo YAML do workflow.
        run_id:        se fornecido, processa só esse run.
    """
    wf = load_workflow(workflow_path)
    runs = db.list_active_runs(run_id=run_id)
    for run in runs:
        try:
            _tick_run(wf, run)
        except Exception as exc:  # noqa: BLE001
            # Falha no próprio tick: registra error_kind=error, não deixa run preso
            _handle_tick_error(run, str(exc))


# ── Tick de um run ─────────────────────────────────────────────────────────

def _tick_run(wf: WorkflowDefinition, run: dict[str, Any]) -> None:
    node_id = run["current_node"]
    if not node_id:
        return

    node_def = wf.get_node(node_id)
    node_type = node_def["type"]

    match node_type:
        case "end":
            _tick_end(run, success=True)
        case "fail":
            _tick_end(run, success=False)
        case "action/script":
            _tick_script(wf, run, node_id, node_def)
        case "action/open_session":
            _tick_open_session(wf, run, node_id, node_def)
        case "action/gate":
            _tick_gate(wf, run, node_id, node_def)
        case "trigger":
            pass  # trigger já foi disparado; aguarda próximo ciclo externo
        case _:
            raise ValueError(f"Tipo de nó desconhecido: '{node_type}'")


# ── Nós terminais (gap #1) ─────────────────────────────────────────────────

def _tick_end(run: dict[str, Any], *, success: bool) -> None:
    """Finaliza o run de forma explícita e intencional."""
    if success:
        db.complete_run(run["id"])
    else:
        db.fail_run(run["id"])


# ── action/script ──────────────────────────────────────────────────────────

def _tick_script(
    wf: WorkflowDefinition,
    run: dict[str, Any],
    node_id: str,
    node_def: dict[str, Any],
) -> None:
    db.start_node(run["id"], node_id)

    cmd = _render(node_def["command"], run, node_def)
    capture = node_def.get("capture_output", False)

    result = subprocess.run(
        cmd, shell=True, capture_output=capture, text=True, timeout=120
    )
    exit_code = result.returncode

    output: dict[str, Any] = {}
    if capture and result.stdout:
        try:
            output = json.loads(result.stdout)
        except json.JSONDecodeError:
            output = {"raw": result.stdout.strip()}

    # Diferenciação de erros (gap #2): exit_code != 0 → error_kind=failed
    error_kind = "failed" if exit_code != 0 else None

    db.complete_node(
        run["id"], node_id,
        exit_status="ok" if exit_code == 0 else "failed",
        output=output,
        exit_code=exit_code,
        error_kind=error_kind,
    )

    conditions = node_def["next"]
    # Para scripts, condição pode checar exit_code ou output.*
    context = {"exit_code": exit_code, "output": output}
    next_node = _resolve_transition(conditions, context)
    db.advance_run(run["id"], next_node)


# ── action/open_session ────────────────────────────────────────────────────

def _tick_open_session(
    wf: WorkflowDefinition,
    run: dict[str, Any],
    node_id: str,
    node_def: dict[str, Any],
) -> None:
    session_key = run.get("session_key")
    if not session_key:
        # Sessão ainda não foi aberta (dispatch pendente) — aguarda próximo tick
        return

    closed, closed_at = _session_is_closed(session_key)

    if not closed:
        # Verificar TTL: se ultrapassou o prazo, tratar como timed_out (gap #2)
        started_at = _node_started_at(run["id"], node_id)
        if started_at and _elapsed_secs(started_at) > run.get("node_ttl_secs", 3600):
            db.complete_node(
                run["id"], node_id,
                exit_status="timed_out",
                output={},
                error_kind="timed_out",   # gap #2
            )
            next_node = _resolve_transition(
                node_def["on_complete"], {"exit_status": "timed_out"}
            )
            db.advance_run(run["id"], next_node)
        return  # ainda ativa e dentro do prazo

    # Sessão encerrada: extrair exit_status do JSONL
    exit_status, output = _extract_exit_status(session_key)

    valid = set(node_def.get("exit_statuses", {}).keys()) | {"failed", "timed_out"}
    if exit_status not in valid:
        exit_status = "failed"

    # error_kind só se o encerramento não foi nominal (gap #2)
    error_kind = exit_status if exit_status in ("failed", "timed_out") else None

    db.complete_node(
        run["id"], node_id,
        exit_status=exit_status,
        output=output,
        error_kind=error_kind,
    )

    next_node = _resolve_transition(
        node_def["on_complete"], {"exit_status": exit_status}
    )
    db.advance_run(run["id"], next_node)


# ── action/gate (gap #3) ───────────────────────────────────────────────────

def _tick_gate(
    wf: WorkflowDefinition,
    run: dict[str, Any],
    node_id: str,
    node_def: dict[str, Any],
) -> None:
    """Aprovação humana sem chat.

    Primeiro tick: cria o token e registra em workflow_gate_tokens.
    Ticks seguintes: verifica se já foi respondido.
    Se expirar sem resposta: error_kind=timed_out.
    """
    db.start_node(run["id"], node_id)

    # Verifica se já existe token para este run+nó
    existing = db.get_gate_token(run["id"], node_id)

    if existing is None:
        # Primeiro tick: criar o token
        ttl = run.get("node_ttl_secs", 3600)
        token = str(uuid.uuid4())
        db.create_gate_token(
            token=token,
            run_id=run["id"],
            node_id=node_id,
            prompt=node_def["prompt"],
            options=node_def.get("options", ["approve", "reject"]),
            ttl_secs=ttl,
        )
        return  # aguarda próximo tick

    decision = existing.get("decision")

    if decision is None:
        # Ainda pendente — verificar expiração (gap #2 para gate)
        expires_at = existing.get("expires_at")
        if expires_at and _is_expired(expires_at):
            db.complete_node(
                run["id"], node_id,
                exit_status="timed_out",
                output={},
                error_kind="timed_out",
            )
            next_node = _resolve_transition(
                node_def["on_complete"], {"exit_status": "timed_out"}
            )
            db.advance_run(run["id"], next_node)
        return  # ainda aguardando decisão

    # Decisão tomada: avançar
    db.complete_node(
        run["id"], node_id,
        exit_status=decision,
        output={"decision": decision, "decided_at": existing.get("decided_at")},
        error_kind=None,
    )
    next_node = _resolve_transition(
        node_def["on_complete"], {"exit_status": decision}
    )
    db.advance_run(run["id"], next_node)


# ── Erro no tick (gap #2: error_kind=error) ───────────────────────────────

def _handle_tick_error(run: dict[str, Any], message: str) -> None:
    """Registra falha inesperada no próprio tick (exceção do engine)."""
    node_id = run.get("current_node", "unknown")
    db.complete_node(
        run["id"], node_id,
        exit_status="error",
        output={"error": message},
        error_kind="error",
    )
    db.fail_run(run["id"])


# ── Resolução de transição ─────────────────────────────────────────────────

def _resolve_transition(
    conditions: list[dict[str, Any]] | str,
    context: dict[str, Any],
) -> str:
    """Retorna o goto do primeiro condition que bate, ou do default.

    conditions pode ser:
      - str: atalho incondicional (next: algum_no)
      - list: [{condition, goto}] com um default obrigatório
    """
    if isinstance(conditions, str):
        return conditions

    for cond in conditions:
        if cond["condition"] == "default":
            return cond["goto"]
        if _eval_condition(cond["condition"], context):
            return cond["goto"]

    raise ValueError(f"Nenhuma condição 'default' encontrada em: {conditions}")


def _eval_condition(condition: str, context: dict[str, Any]) -> bool:
    """Avalia condições simples: 'exit_status == pr_opened', 'exit_code == 0', 'output.has_work == true'."""
    parts = condition.split()
    if len(parts) != 3:
        return False
    lhs_path, op, rhs = parts
    if op != "==":
        return False

    # Resolve lhs pelo contexto (suporte a dot notation: output.has_work)
    lhs_val = _resolve_path(lhs_path, context)

    # Normaliza rhs
    if rhs == "true":
        rhs_val: Any = True
    elif rhs == "false":
        rhs_val = False
    elif rhs.lstrip("-").isdigit():
        rhs_val = int(rhs)
    else:
        rhs_val = rhs

    return lhs_val == rhs_val


def _resolve_path(path: str, context: dict[str, Any]) -> Any:
    """Resolve 'exit_status', 'exit_code', 'output.has_work' etc. no context."""
    parts = path.split(".")
    val: Any = context
    for part in parts:
        if not isinstance(val, dict):
            return None
        val = val.get(part)
    return val


# ── Leitura de sessão (filesystem JSONL) ──────────────────────────────────

def _session_is_closed(session_key: str) -> tuple[bool, float | None]:
    jsonl = SESSIONS_DIR / f"{session_key}.jsonl"
    if not jsonl.exists():
        return False, None
    try:
        first = jsonl.open().readline()
        meta = json.loads(first)
    except (json.JSONDecodeError, OSError):
        return False, None
    if meta.get("_type") != "metadata":
        return False, None
    return meta.get("closed", False), meta.get("closed_at")


def _extract_exit_status(session_key: str) -> tuple[str, dict[str, Any]]:
    jsonl = SESSIONS_DIR / f"{session_key}.jsonl"
    try:
        lines = jsonl.read_text().splitlines()
    except OSError:
        return "failed", {}
    for raw in reversed(lines):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("role") != "assistant":
            continue
        match = EXIT_PATTERN.search(msg.get("content", ""))
        if match:
            try:
                data = json.loads(match.group(1))
                return data.get("exit_status", "failed"), data.get("output", {})
            except json.JSONDecodeError:
                continue
    return "failed", {}


# ── Helpers de tempo ──────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed_secs(iso_ts: str) -> float:
    try:
        started = datetime.fromisoformat(iso_ts)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return (_now_utc() - started).total_seconds()
    except ValueError:
        return 0.0


def _is_expired(iso_ts: str) -> bool:
    try:
        expires = datetime.fromisoformat(iso_ts)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return _now_utc() > expires
    except ValueError:
        return False


def _node_started_at(run_id: str, node_id: str) -> str | None:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT started_at FROM workflow_node_states WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
    return row["started_at"] if row else None


# ── Render de template ────────────────────────────────────────────────────

def _render(template: str, run: dict[str, Any], node_def: dict[str, Any]) -> str:
    """Substitui {variável} no template com valores do context do nó."""
    context = node_def.get("context", {})
    result = template
    for key, val in context.items():
        result = result.replace(f"{{{key}}}", str(val) if val is not None else "")
    return result
