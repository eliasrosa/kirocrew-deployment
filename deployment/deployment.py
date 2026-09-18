"""KiroCrew Flow — cron de scan e dispatch one-shot.

Cron de SCRIPT do Kiro Crew (sem LLM, zero token no polling).

  - Fase 1 (auto_dispatch=false): só AVISA — você aciona manual.
  - Fase 2 (auto_dispatch=true ): dispara sessão ONE-SHOT que implementa e abre PR.

Agora usa a arquitetura hexagonal de flow/:
  scan_candidates() ← flow/scan/scanner.py  → filtra issues candidatas (zero token)
  provider_for()    ← flow/ports/           → adapter GitHub ou Jira
  can_leave_spec()  ← flow/domain/gates.py → validação GATE 1 (zero token)

O dispatch (chamada de sessão one-shot) ainda vive aqui — é o driving adapter da Fase 1.

Depende do Kiro Crew rodando (loopback interno). NÃO é standalone.

Registro (uma vez):
    cron_add(name="crewflow-scan", script="~/.kiro/crew/crons/deployment.py:run", every=600)

Config: ~/.kiro/crew/crons/deployment.config.yaml (copie de config.example.yaml).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
import sys

logger = logging.getLogger(__name__)

# ── Adiciona o diretório raiz do repo ao path para importar flow/ ─────────
# Necessário porque o cron do Kiro Crew executa o arquivo diretamente e
# flow/ não está instalado como pacote no Python do sistema.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from flow.ports.issue_provider import provider_for  # noqa: E402
from flow.scan.cache import open_cache  # noqa: E402
from flow.scan.scanner import scan_candidates  # noqa: E402

# ── Labels (mantidas para o prompt de dispatch) ───────────────────────────
LABEL_DEV     = "crewflow:dev"
LABEL_REVIEW  = "crewflow:review"
LABEL_RUNNING = "crewflow:running"
LABEL_BLOCKED = "crewflow:blocked"

# ── Carregamento de config ────────────────────────────────────────────────
_CONFIG_CANDIDATES = [
    os.path.join(_HERE, "deployment.config.yaml"),
    os.path.expanduser("~/.kiro/crew/crons/deployment.config.yaml"),
]


def _load_config() -> dict:
    """Lê a config YAML (parser mínimo, sem dependência externa)."""
    path = next((p for p in _CONFIG_CANDIDATES if os.path.exists(p)), None)
    if not path:
        raise RuntimeError(
            "esteira: config não encontrada. Copie config.example.yaml para "
            "deployment.config.yaml ao lado do script."
        )
    try:
        import yaml  # type: ignore[import-untyped]
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return _mini_yaml(path)


def _mini_yaml(path: str) -> dict:
    """Parser YAML minimalista para quando PyYAML não está disponível."""
    cfg: dict = {}
    cur_key: str | None = None

    def _coerce(val: str) -> object:
        if val.lower() in ("true", "false"):
            return val.lower() == "true"
        if val.isdigit():
            return int(val)
        return val

    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue

            indented = line.startswith((" ", "\t"))
            stripped = line.strip()

            if stripped.startswith("- ") and cur_key and isinstance(cfg.get(cur_key), list):
                cfg[cur_key].append(stripped[2:].strip().strip('"\''))
                continue

            if indented and ":" in stripped and cur_key:
                if not isinstance(cfg.get(cur_key), dict):
                    if cfg.get(cur_key):
                        continue
                    cfg[cur_key] = {}
                k, _, v = stripped.partition(":")
                v = v.strip().strip('"\'')
                if v != "":
                    cfg[cur_key][k.strip().strip('"\'')] = _coerce(v)
                continue

            if ":" in line and not indented:
                key, _, val = line.partition(":")
                key, raw_val = key.strip(), val.strip()
                val = raw_val.strip('"\'')
                quoted_empty = val == "" and raw_val in ('""', "''")
                if val == "" and not quoted_empty:
                    cfg[key], cur_key = [], key
                else:
                    cur_key = None
                    cfg[key] = "" if quoted_empty else _coerce(val)
    return cfg


# ── Sessões ativas ────────────────────────────────────────────────────────

def _sessdir() -> str:
    return os.path.expanduser("~/.kiro/crew/sessions")


def _active_sessions() -> int:
    return len(glob.glob(os.path.join(_sessdir(), "dashboard_esteira-*.jsonl.lock")))


def _repo_has_active(repo: str) -> bool:
    short = repo.split("/")[-1]
    return bool(glob.glob(os.path.join(_sessdir(), f"dashboard_esteira-{short}-*.jsonl.lock")))


# ── Prompt de dispatch ────────────────────────────────────────────────────

def _dispatch_prompt(repo: str, issue: dict, cfg: dict) -> str:
    short = repo.split("/")[-1]
    vault = cfg.get("vault_root") or ""
    dev_root = cfg.get("dev_root") or os.path.expanduser("~/dev")
    chat_id = cfg.get("notify_chat_id") or ""
    vault_step = ""
    if vault:
        vault_step = (
            f"   - VAULT: edite `{vault}/Projetos/{short}/backlog.md` refletindo a issue "
            f"resolvida e sincronize com `sh {vault}/.sync.sh \"<msg>\"` (NUNCA `git push` "
            "literal). Se a pasta não existir, pule sem erro.\n"
        )
    notify_step = (
        f"avise via voice_maybe (chat_id {chat_id}, intent auto) com TL;DR, "
        if chat_id else "reporte o resultado, "
    )
    return (
        "------------ AGENT HEADER ----------------\n"
        f"REPO: {repo}\n"
        f"ISSUE: #{issue['number']} — {issue['title']}\n"
        f"URL: {issue['url']}\n"
        f"SESSION TITLE: {short} #{issue['number']}: {issue['title']}\n"
        "------------ CONTEXT TASK ----------------\n"
        "Você é um agente de implementação ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.\n\n"
        "FLUXO (execute UMA vez, do início ao fim, e PARE):\n"
        "0. TÍTULO: como PRIMEIRA ação, defina o título da sessão = `SESSION TITLE`.\n"
        "1. Leia a issue (gh issue view) e a doc do repo (.kiro/steering/, README).\n"
        "2. ESCOPO: se a issue exige decisão de design não-tomada ou é vaga, NÃO implemente — "
        f"comente, marque `{LABEL_BLOCKED}`, avise e ENCERRE.\n"
        f"3. Marque `{LABEL_DEV}` + `{LABEL_RUNNING}`. NÃO faça `git clone`. Use o clone em "
        f"`{dev_root}/{short}` como base e crie um WORKTREE ISOLADO.\n"
        f"   A branch base é a DEFAULT DO REPO — descubra, não presuma:\n"
        f"   `BASE=$(gh repo view {repo} --json defaultBranchRef --jq .defaultBranchRef.name)`\n"
        f"   `cd {dev_root}/{short} && git fetch origin && git worktree add -b "
        f"feat/issue-{issue['number']} {dev_root}/.esteira-worktrees/{short}-{issue['number']} "
        f"\"origin/$BASE\"`\n"
        "   Trabalhe DENTRO do worktree; remova-o ao fim. NUNCA toque em outros worktrees.\n"
        "4. Implemente EXATAMENTE o escopo — nada além.\n"
        "5. Valide localmente (build/testes). Se falhar e não conseguir corrigir, "
        f"pare em `{LABEL_BLOCKED}`.\n"
        f"6. Abra PR com 'Closes #{issue['number']}' e troque a label para `{LABEL_REVIEW}`. "
        "**NUNCA mergeie. NUNCA faça deploy.** Ambos são ações humanas manuais.\n"
        f"7. Ao terminar: {notify_step}remova `{LABEL_RUNNING}` (mantenha `{LABEL_REVIEW}`), "
        "e ENCERRE.\n"
        f"{vault_step}\n"
        "REGRAS CRÍTICAS:\n"
        "- UMA passada. Terminou, acabou. NÃO entre em loop.\n"
        "- NUNCA mergeie. NUNCA faça deploy.\n"
        f"- Se bloquear, marque `{LABEL_BLOCKED}`, avise, e pare.\n"
        "------------------------------------------"
    )


def _dispatch(ctx: object, repo: str, issue: dict, cfg: dict) -> None:
    """Fire-and-forget POST /api/chat (loopback interno)."""
    import urllib.request as _u
    slot = f"esteira-{repo.split('/')[-1]}-{issue['number']}"
    body = json.dumps({
        "message": _dispatch_prompt(repo, issue, cfg),
        "agent": cfg.get("agent") or "kirocrew",
        "slot": slot,
        "memory_mode": "temporary",
    }).encode()
    req = _u.Request(
        f"http://localhost:{ctx._port}/api/chat",  # type: ignore[attr-defined]
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": ctx._secret,  # type: ignore[attr-defined]
            "X-Session-Key": f"cron:{ctx.job.id}",  # type: ignore[attr-defined]
        },
        method="POST",
    )
    try:
        from kiro_crew.loopback_http import loopback_urlopen  # type: ignore[import]
        with loopback_urlopen(req, timeout=3) as resp:
            resp.read(1)
    except Exception:
        pass


# ── Conversão ScanResult → formato legado do dispatch ────────────────────

def _scan_result_to_issue(result: object) -> dict:
    """Converte um ScanResult para o formato mínimo que o prompt de dispatch precisa."""
    from flow.scan.scanner import ScanResult
    r: ScanResult = result  # type: ignore[assignment]
    item = r.item
    # Extrai o número da issue key ("VGAT-123" → 123, "owner/repo#42" → 42)
    import re as _re
    m = _re.search(r"[#\-/](\d+)$", item.key)
    number = int(m.group(1)) if m else 0
    return {
        "number": number,
        "title": item.title,
        "url": item.key,  # key é a URL canônica no adapter GitHub
    }


# ── Ponto de entrada do cron ──────────────────────────────────────────────

def run(ctx: object) -> None:
    cfg = _load_config()
    repos: list[str] = cfg.get("repos") or []
    auto = bool(cfg.get("auto_dispatch", False))
    max_conc = int(cfg.get("max_concurrent", 2))
    one_per_repo = bool(cfg.get("one_per_repo", True))
    chat_id = cfg.get("notify_chat_id") or ""
    issue_provider_name: str = cfg.get("issue_provider", "github")

    if not repos:
        logger.warning("deployment: nenhum repo/projeto configurado")
        return

    # ── Carrega a SquadConfig: arquivo squads/<id>.yaml > inline da config ─
    from flow.config.squad import SquadConfig, SquadConfigError, load_squad

    squad: SquadConfig | None = None
    squad_file = cfg.get("squad_config")   # caminho opcional na config
    if squad_file and os.path.exists(squad_file):
        try:
            squad = load_squad(squad_file)
            logger.info("deployment: squad carregada de %s (%s)", squad_file, squad.id)
        except SquadConfigError as exc:
            logger.warning("deployment: falha ao carregar squad config: %s", exc)

    if squad is None:
        # Constrói inline a partir da config legada
        from flow.config.squad import _parse_squad
        raw: dict = {
            "id": cfg.get("squad_id", "default"),
            "issue_provider": issue_provider_name,
            "repos": repos,
            "workflow_template": cfg.get("workflow_template", "versao-c"),
        }
        if cfg.get("project"):
            raw["project"] = cfg["project"]
        raw_params = cfg.get("workflow_params") or {}
        if raw_params:
            raw["workflow_params"] = raw_params
        raw_routing = cfg.get("routing") or []
        if raw_routing:
            raw["routing"] = raw_routing
        try:
            squad = _parse_squad(raw)
            logger.debug("deployment: squad construída inline (id=%s)", squad.id)
        except SquadConfigError as exc:
            logger.error("deployment: squad config inválida: %s", exc)

    # ── Inicializa o provider e o cache ────────────────────────────────────
    provider = provider_for(issue_provider_name)

    # Usa SquadScanConfig como adaptador entre SquadConfig e scan_candidates
    # (scan_candidates aceita qualquer objeto com squad_id, issue_provider,
    #  projects e repos — SquadConfig satisfaz essa interface)
    if squad is not None:
        from flow.scan.scanner import SquadScanConfig
        scan_cfg = SquadScanConfig(
            squad_id=squad.id,
            issue_provider=squad.issue_provider,
            projects=tuple(squad.projects),
            repos=squad.repos,
        )
    else:
        from flow.scan.scanner import SquadScanConfig
        scan_cfg = SquadScanConfig(
            squad_id=cfg.get("squad_id", "default"),
            issue_provider=issue_provider_name,
            projects=tuple(repos),
            repos=frozenset(repos),
        )

    conn: sqlite3.Connection = open_cache(scan_cfg.squad_id)

    # ── Executa o scan zero-token ─────────────────────────────────────────
    try:
        scan_results = scan_candidates(scan_cfg, provider, conn)
    except Exception as exc:
        logger.error("deployment: erro no scan: %s", exc)
        from kiro_crew.cron import Skip  # type: ignore[import]
        raise Skip() from exc
    finally:
        conn.close()

    # ── Separa candidatos de dispatch dos informativos ────────────────────
    # ── Passa todos os resultados pelo executor ────────────────────────────
    from flow.executor.executor import ActionKind, decide, resolve_template
    from flow.scan.scanner import ScanResult

    # Categorias de resultado após o executor — tipadas para mypy
    spec_invalid: list[ScanResult] = []
    dispatch_devs: list[tuple[str, dict, object]] = []   # (repo, issue, decision)
    dispatch_reviewers: list[tuple[str, dict]] = []      # (repo, issue)
    needs_human: list[tuple[ScanResult, object]] = []    # (result, decision)
    blocked_bypass: list[ScanResult] = []                # result com bypass sem justif
    rebranded: list[tuple[ScanResult, object]] = []      # (result, decision)

    for result in scan_results:
        # Flags do scan que não precisam do executor
        if result.spec_valid is False:
            spec_invalid.append(result)
            continue

        # Passa pelo executor para decisão completa
        # Lê o comentário de estado se precisar (bypass, COV) — só faz a
        # chamada de I/O quando o estado pode precisar dele
        from flow.domain.state import Modifier, State
        needs_comment = (
            Modifier.HML_BYPASS in result.modifiers
            or (hasattr(result.current_state, "__eq__") and result.current_state is State.DEV)
        )
        state_comment: str | None = None
        if needs_comment:
            import contextlib
            with contextlib.suppress(Exception):
                state_comment = provider.get_state_comment(
                    result.item.key.split("/issues/")[0].replace("https://github.com/", "")
                    or (repos[0] if repos else ""),
                    result.item.key,
                )

        decision = decide(result, state_comment=state_comment, squad=squad)

        # Loga o template resolvido pelo executor e a ação decidida, para
        # cada issue processada — facilita debugar por que uma issue foi para
        # REBRAND (GATE 0) em vez de DISPATCH_DEV.
        template = resolve_template(result, squad)
        logger.info(
            "deployment: issue=%s template=%s action=%s",
            result.item.key,
            template,
            decision.action,
        )

        if decision.action is ActionKind.SKIP:
            continue
        if decision.action is ActionKind.BLOCK:
            blocked_bypass.append(result)
        elif decision.action is ActionKind.REBRAND:
            rebranded.append((result, decision))
        elif decision.action is ActionKind.NOTIFY_HUMAN:
            needs_human.append((result, decision))
        elif decision.action is ActionKind.DISPATCH_REVIEWER:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            issue = _scan_result_to_issue(result)
            dispatch_reviewers.append((repo, issue))
        elif decision.action is ActionKind.DISPATCH_DEV:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            if not repo:
                repo = repos[0] if repos else ""
            issue = _scan_result_to_issue(result)
            dispatch_devs.append((repo, issue, decision))

    # Sem nada a fazer?
    if not any([spec_invalid, dispatch_devs, dispatch_reviewers,
                needs_human, blocked_bypass, rebranded]):
        return

    # ── Notifica specs inválidas ──────────────────────────────────────────
    if spec_invalid:
        vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
        linhas = "\n".join(
            f"  - {r.item.key}: {r.item.title}"
            for r in spec_invalid
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: {len(spec_invalid)} spec(s) sem repo declarado — "
            f"corrija o título antes de priorizar.{vm}\n{linhas}"
        )

    # ── Notifica bypass bloqueados ────────────────────────────────────────
    if blocked_bypass:
        vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
        linhas = "\n".join(f"  - {r.item.key}: {r.item.title}" for r in blocked_bypass)
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: merge bloqueado — hml-bypass sem justificativa.{vm}\n{linhas}"
        )

    # ── Aplica rebranding (troca de template) ─────────────────────────────
    for result, decision in rebranded:  # type: ignore[assignment]
        try:
            # Atualiza as labels para refletir o novo template
            current_labels = list(result.item.labels)
            for lbl in decision.remove_labels:
                if lbl in current_labels:
                    current_labels.remove(lbl)
            current_labels.extend(decision.add_labels)
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            provider.set_labels(repo, result.item.key, current_labels)
            logger.info("deployment: rebrand %s → %s", result.item.key, decision.new_template)
        except Exception as exc:
            logger.error("deployment: erro no rebrand de %s: %s", result.item.key, exc)

    # ── Dispatch do executor de dev ───────────────────────────────────────
    vagas = (max_conc - _active_sessions()) if auto else 0
    disparadas: list[tuple[str, dict]] = []
    adiadas: list[tuple[str, dict]] = []

    for repo, issue, _decision in dispatch_devs:
        if not auto:
            adiadas.append((repo, issue))
            continue
        if vagas <= 0:
            adiadas.append((repo, issue))
            continue
        if one_per_repo and _repo_has_active(repo):
            adiadas.append((repo, issue))
            continue
        try:
            _dispatch(ctx, repo, issue, cfg)
            disparadas.append((repo, issue))
            vagas -= 1
        except Exception as exc:
            logger.error("deployment: erro ao despachar %s: %s", issue.get("number"), exc)
            adiadas.append((repo, issue))

    # ── Notificação de resultado de dispatch ──────────────────────────────
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    if auto and disparadas:
        linhas = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in disparadas)
        extra = ""
        if adiadas:
            fila = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in adiadas)
            extra = f"\n\nNA FILA:\n{fila}"
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: disparei sessão(ões) one-shot.{vm}\n{linhas}{extra}"
        )
    elif auto and adiadas:
        fila = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in adiadas)
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: {len(adiadas)} issue(s) na fila (limite cheio).{vm}\n{fila}"
        )
    elif not auto and dispatch_devs:
        blocos = "\n".join(
            f"  - {repo}#{issue['number']}: {issue['title']}"
            for repo, issue, _ in dispatch_devs
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow (Fase 1): {len(dispatch_devs)} issue(s) prontas.{vm}\n{blocos}"
        )

    # Notificações para humanos (NOTIFY_HUMAN)
    if needs_human:
        _notify_human_actions(ctx, needs_human, chat_id)

    if dispatch_reviewers:
        _notify_reviewers(ctx, dispatch_reviewers, chat_id)


def _notify_human_actions(ctx: object, items: list, chat_id: str) -> None:
    """Notifica o humano certo sobre ações pendentes."""
    from flow.executor.executor import HumanRole
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    # Agrupa por papel
    tl_items = [(r, d) for r, d in items if getattr(d, "notify_role", None) is HumanRole.TL]
    dev_items = [(r, d) for r, d in items if getattr(d, "notify_role", None) is HumanRole.DEV]
    qa_items = [(r, d) for r, d in items if getattr(d, "notify_role", None) is HumanRole.QA]

    if tl_items:
        linhas = "\n".join(f"  - {r.item.key}: {r.item.title} — {d.reason}" for r, d in tl_items)
        ctx.notify(f"KiroCrew Flow: aguarda ação do TL.{vm}\n{linhas}")  # type: ignore[attr-defined]
    if dev_items:
        linhas = "\n".join(f"  - {r.item.key}: {r.item.title} — {d.reason}" for r, d in dev_items)
        ctx.notify(f"KiroCrew Flow: aguarda ação do Dev.{vm}\n{linhas}")  # type: ignore[attr-defined]
    if qa_items:
        linhas = "\n".join(f"  - {r.item.key}: {r.item.title} — {d.reason}" for r, d in qa_items)
        ctx.notify(f"KiroCrew Flow: aguarda ação do QA.{vm}\n{linhas}")  # type: ignore[attr-defined]


def _notify_reviewers(ctx: object, items: list, chat_id: str) -> None:
    """Notifica que o kiro-reviewer foi disparado."""
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
    linhas = "\n".join(f"  - {repo}#{issue['number']}: {issue['title']}" for repo, issue in items)
    ctx.notify(  # type: ignore[attr-defined]
        f"KiroCrew Flow: análise automatizada de code review disparada.{vm}\n{linhas}"
    )
