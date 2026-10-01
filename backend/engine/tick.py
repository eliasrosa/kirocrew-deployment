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

import glob
import json
import re
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import request as _urllib

from backend.engine import db
from backend.engine.loader import WorkflowDefinition
from backend.engine.loader import load as load_workflow

# Dir de sessões do Kiro Crew
SESSIONS_DIR = Path.home() / ".kiro/crew/sessions"

# Regex para extrair WORKFLOW_EXIT da última mensagem do agente
EXIT_PATTERN = re.compile(r"WORKFLOW_EXIT:\s*(\{.*?\})", re.DOTALL)

# Agente padrão para sessões one-shot do engine
_DEFAULT_AGENT = "kirocrew"


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
        except Exception as exc:
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
        cmd, shell=True, capture_output=capture, text=True, timeout=120, check=False
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
        # Nenhuma sessão aberta ainda -- fazer o dispatch agora
        db.start_node(run["id"], node_id)
        dispatched_key = _dispatch_open_session(run, node_id, node_def, wf)
        if dispatched_key:
            db.set_run_session_key(run["id"], dispatched_key)
        # Seja sucesso ou falha temporária, aguarda próximo tick para verificar
        return

    closed, _closed_at = _session_is_closed(session_key)

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


# ── Dispatch de sessão one-shot ────────────────────────────────────────────

def _dispatch_open_session(
    run: dict[str, Any],
    node_id: str,
    node_def: dict[str, Any],
    wf: WorkflowDefinition,
) -> str | None:
    """Abre a sessão one-shot para um nó action/open_session.

    Fluxo:
      1. Renderiza a instrução (instruction_path + context do YAML + seção WORKFLOW_EXIT)
      2. POST /api/chat/slots  → cria o slot
      3. POST /api/chat        → envia a mensagem com slot_key no body
      4. Retorna o slot_key (session_key) para gravar no run

    Retorna None em caso de falha (fire-and-forget -- próximo tick retenta).
    """

    slot = f"engine-{run['id'][:8]}-{node_id}"
    agent = node_def.get("agent", _DEFAULT_AGENT)

    # Monta a instrução
    message = _render_instruction(run, node_id, node_def, wf)
    if not message:
        return None  # template não encontrado -- sem dispatch

    port = _get_gateway_port()
    secret = _get_gateway_secret(port)
    if not port or not secret:
        return None

    base_url = f"http://localhost:{port}"
    headers = {
        "Content-Type": "application/json",
        "X-Internal-Secret": secret,
    }

    # Step 1: criar o slot
    slot_body = json.dumps({"name": slot, "agent": agent, "memory_mode": "temporary"}).encode()
    slot_req = _urllib.Request(
        f"{base_url}/api/chat/slots",
        data=slot_body,
        headers=headers,
        method="POST",
    )
    try:
        with _urllib.urlopen(slot_req, timeout=10) as resp:
            resp.read(1)
    except _urllib.HTTPError as exc:
        if exc.code != 409:
            return None  # falha real
        # 409 = slot já existe (tick anterior criou mas não gravou o session_key)
    except Exception:
        return None

    # Step 2: enviar a mensagem
    chat_body = json.dumps({"slot_key": slot, "message": message, "agent": agent}).encode()
    chat_req = _urllib.Request(
        f"{base_url}/api/chat",
        data=chat_body,
        headers=headers,
        method="POST",
    )
    try:
        with _urllib.urlopen(chat_req, timeout=12) as resp:
            resp.read(1)
    except Exception:
        # Limpar slot órfão
        _delete_slot(base_url, headers, slot)
        return None

    # O session_key que o gateway cria para esta sessão é derivado do slot name
    # Padrão: "dashboard_chat-{slot_name}-{timestamp}" -- não temos o timestamp aqui.
    # Usamos o slot como chave de lookup: ao verificar fechamento, procuramos o
    # arquivo JSONL cujo metadata.slot == slot.
    return slot  # armazena o slot como chave de busca no filesystem


def _render_instruction(
    run: dict[str, Any],
    node_id: str,
    node_def: dict[str, Any],
    wf: WorkflowDefinition,
) -> str | None:
    """Renderiza a instrução do nó: lê o template e injeta contexto + seção WORKFLOW_EXIT."""
    instruction_path = node_def.get("instruction_path", "")
    if not instruction_path:
        return None

    # Resolve relativo ao repo do kirocrew-flow
    app_root = Path(__file__).parent.parent.parent
    full_path = app_root / instruction_path
    if not full_path.exists():
        return None

    template = full_path.read_text()

    # Substitui variáveis de contexto: {issue_key}, {repo}, {run_id}, {node_id}
    context = _resolve_context(node_def.get("context", {}), run)
    context["run_id"] = run["id"]
    context["node_id"] = node_id
    for key, val in context.items():
        template = template.replace(f"{{{key}}}", str(val) if val is not None else "")

    # Injeta seção de encerramento obrigatória com os exit_statuses válidos
    exit_statuses = list(node_def.get("exit_statuses", {}).keys())
    exit_section = _exit_section(run["id"], node_id, exit_statuses)
    return f"{template}\n\n{exit_section}"


def _resolve_context(raw_context: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """Resolve referências $nodes.*.output.* no context do YAML contra o SQLite."""
    resolved: dict[str, Any] = {}
    for key, val in raw_context.items():
        if isinstance(val, str) and val.startswith("$nodes."):
            # Ex: "$nodes.check_dispatchable.output.issue_key"
            parts = val.lstrip("$").split(".")
            # parts = ["nodes", "check_dispatchable", "output", "issue_key"]
            if len(parts) >= 4 and parts[0] == "nodes" and parts[2] == "output":
                ref_node = parts[1]
                field = ".".join(parts[3:])
                resolved[key] = _get_node_output_field(run["id"], ref_node, field)
            else:
                resolved[key] = val
        else:
            resolved[key] = val
    return resolved


def _get_node_output_field(run_id: str, node_id: str, field: str) -> Any:
    """Lê um campo do output_json de um nó completado."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT output_json FROM workflow_node_states WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
    if not row:
        return None
    try:
        output = json.loads(row["output_json"] or "{}")
        parts = field.split(".")
        val: Any = output
        for part in parts:
            if not isinstance(val, dict):
                return None
            val = val.get(part)
        return val
    except (json.JSONDecodeError, TypeError):
        return None


def _exit_section(run_id: str, node_id: str, exit_statuses: list[str]) -> str:
    """Gera a seção obrigatória de encerramento injetada no final do template."""
    options = " | ".join(f"`{s}`" for s in exit_statuses) if exit_statuses else "`failed`"
    app_root = Path(__file__).parent.parent.parent
    app_root / ".kiro/crew/runs" / run_id / f"{node_id}.exit.json"
    return f"""---
## Encerramento obrigatório

Ao concluir, declare o resultado na **última linha** da sua resposta final:

`WORKFLOW_EXIT: {{"exit_status": "<status>", "output": {{}}}}`

**Exit statuses válidos para este nó:** {options}

**run_id:** `{run_id}` | **node_id:** `{node_id}`

Não encerre o turno sem essa linha. Em caso de erro inesperado, use `failed`.
---"""


# ── Helpers de gateway (port/secret) ──────────────────────────────────────

def _get_gateway_port() -> int | None:
    """Detecta a porta do gateway via socket file (igual ao deployment.py)."""
    pattern = str(Path.home() / ".kiro/crew/dashboard-*.sock")
    socks = glob.glob(pattern)
    if socks:
        m = re.search(r"dashboard-(\d+)\.sock", socks[0])
        if m:
            return int(m.group(1))
    return 5478  # fallback


def _get_gateway_secret(port: int | None) -> str:
    """Lê o secret do gateway na ordem de precedência correta (igual ao deployment.py)."""
    if port:
        run_secret = Path.home() / f".kiro/crew/run/gateway-{port}.secret"
        if run_secret.exists():
            return run_secret.read_text().strip()
    local_secret = Path.home() / ".kiro/crew/.local_secret"
    if local_secret.exists():
        return local_secret.read_text().strip()
    return ""


def _delete_slot(base_url: str, headers: dict[str, str], slot: str) -> None:
    """Tenta deletar um slot órfão (fire-and-forget, ignora erros)."""
    try:
        req = _urllib.Request(
            f"{base_url}/api/chat/slots/{slot}",
            headers=headers,
            method="DELETE",
        )
        with _urllib.urlopen(req, timeout=5) as resp:
            resp.read(1)
    except Exception:
        pass


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
    """Verifica se a sessão encerrou lendo o header JSONL.

    Aceita dois formatos de session_key:
      1. Nome exato do arquivo JSONL (ex: "dashboard_chat-42-1790808147")
      2. Slot name (ex: "engine-abc12345-stage_briefing") -- faz glob para achar o JSONL
    """
    # Tenta arquivo exato primeiro
    jsonl = SESSIONS_DIR / f"{session_key}.jsonl"
    if not jsonl.exists():
        # Busca por slot: o gateway nomeia o arquivo com base no slot name
        matches = list(SESSIONS_DIR.glob(f"*{session_key}*.jsonl"))
        if not matches:
            return False, None
        jsonl = matches[0]

    try:
        first = jsonl.open().readline()
        meta = json.loads(first)
    except (json.JSONDecodeError, OSError):
        return False, None
    if meta.get("_type") != "metadata":
        return False, None
    return meta.get("closed", False), meta.get("closed_at")


def _extract_exit_status(session_key: str) -> tuple[str, dict[str, Any]]:
    """Lê WORKFLOW_EXIT da última mensagem do agente no JSONL da sessão."""
    jsonl = SESSIONS_DIR / f"{session_key}.jsonl"
    if not jsonl.exists():
        matches = list(SESSIONS_DIR.glob(f"*{session_key}*.jsonl"))
        if not matches:
            return "failed", {}
        jsonl = matches[0]

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
    return datetime.now(UTC)


def _elapsed_secs(iso_ts: str) -> float:
    try:
        started = datetime.fromisoformat(iso_ts)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return (_now_utc() - started).total_seconds()
    except ValueError:
        return 0.0


def _is_expired(iso_ts: str) -> bool:
    try:
        expires = datetime.fromisoformat(iso_ts)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
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
