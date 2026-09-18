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

from datetime import UTC  # noqa: E402

from flow.audit.state_comment import (  # noqa: E402
    render_issue_pr_reference,
    render_pr_review_comment,
)
from flow.ports.issue_provider import provider_for  # noqa: E402
from flow.scan.cache import open_cache  # noqa: E402
from flow.scan.scanner import ScanResult, scan_candidates  # noqa: E402

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
    """Parser YAML minimalista para quando PyYAML não está disponível.

    Delega para o parser compartilhado em ``flow.config.squad`` para que a
    config do cron e a config de squad tenham exatamente o mesmo suporte de
    schema (incluindo `routing:` na forma multi-linha com `match.labels`
    aninhado). Para squads com routing complexo, PyYAML continua recomendado
    (`pip install -e '.[yaml]'`).
    """
    from pathlib import Path

    from flow.config.squad import _mini_yaml as _shared_mini_yaml
    return dict(_shared_mini_yaml(Path(path)))


# ── Sessões ativas ────────────────────────────────────────────────────────

def _sessdir() -> str:
    return os.path.expanduser("~/.kiro/crew/sessions")


_LOCK_STALE_SECS = 2 * 3600  # locks mais velhos que 2h são considerados obsoletos


def _lock_is_stale(path: str) -> bool:
    """Retorna True se o arquivo de lock existe mas é antigo (sessão provavelmente encerrada)."""
    try:
        age = __import__("time").time() - os.path.getmtime(path)
        return age > _LOCK_STALE_SECS
    except OSError:
        # Arquivo desapareceu entre o glob e a stat — trata como ausente (não ativo).
        return True


def _active_sessions() -> int:
    locks = glob.glob(os.path.join(_sessdir(), "dashboard_esteira-*.jsonl.lock"))
    return sum(1 for p in locks if not _lock_is_stale(p))


def _repo_has_active(repo: str) -> bool:
    short = repo.split("/")[-1]
    locks = glob.glob(os.path.join(_sessdir(), f"dashboard_esteira-{short}-*.jsonl.lock"))
    return any(not _lock_is_stale(p) for p in locks)


# ── Prompt de dispatch ────────────────────────────────────────────────────

def _dispatch_prompt(
    repo: str,
    issue: dict,
    cfg: dict,
    prompt_extra: str = "",
) -> str:
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
        "1. CONTEXTO: leia TODA a documentação do repo antes de qualquer ação:\n"
        f"   - `.kiro/steering/*.md` (steerings do projeto)\n"
        f"   - `README.md`\n"
        f"   - `docs/` se existir\n"
        f"   - A própria issue: `gh issue view {issue['number']} --repo {repo}`\n"
        "   Não pule esta etapa — as steerings têm convenções e gotchas críticos.\n"
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
        "5. DOCS: atualize README, steerings e docs/ se a mudança afeta comportamento, "
        "arquitetura ou convenções. Não atualize se a mudança for puramente interna (bugfix, refactor).\n"
        "6. Valide localmente (build/testes). Se falhar e não conseguir corrigir, "
        f"pare em `{LABEL_BLOCKED}`.\n"
        f"7. Abra PR com 'Closes #{issue['number']}' e troque a label para `{LABEL_REVIEW}`. "
        "Após abrir o PR, ATUALIZE o título da sessão adicionando o número do PR: "
        f"`{short} #{issue['number']} #<N-PR>: {issue['title']}`. "
        "**NUNCA mergeie. NUNCA faça deploy.** Ambos são ações humanas manuais.\n"
        f"8. Ao terminar: {notify_step}remova `{LABEL_RUNNING}` (mantenha `{LABEL_REVIEW}`), "
        "e ENCERRE.\n"
        f"{vault_step}\n"
        "REGRAS CRÍTICAS:\n"
        "- UMA passada. Terminou, acabou. NÃO entre em loop.\n"
        "- NUNCA mergeie. NUNCA faça deploy.\n"
        f"- Se bloquear, marque `{LABEL_BLOCKED}`, avise, e pare.\n"
        "------------------------------------------"
        + (f"\n\n{prompt_extra.strip()}" if prompt_extra.strip() else "")
    )


def _pr_exists(repo: str, issue_number: int) -> bool:
    """Retorna True se já existe um PR aberto para a branch feat/issue-<N> neste repo.

    Previne que a sessão one-shot abra um segundo PR quando a primeira branch
    já está em review (ex.: conflito de merge na primeira tentativa).
    """
    import subprocess
    branch = f"feat/issue-{issue_number}"
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--head", branch,
             "--state", "open", "--json", "number"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "deployment: gh pr list falhou para %s head=%s: %s",
                repo, branch, result.stderr.strip(),
            )
            return False
        import json as _json
        prs = _json.loads(result.stdout or "[]")
        if prs:
            logger.info(
                "deployment: PR já existe para %s#%s (branch %s) — dispatch ignorado",
                repo, issue_number, branch,
            )
            return True
        return False
    except Exception as exc:
        logger.warning(
            "deployment: erro ao verificar PR existente para %s#%s: %s",
            repo, issue_number, exc,
        )
        return False


def _dispatch(
    ctx: object,
    repo: str,
    issue: dict,
    cfg: dict,
    prompt_extra: str = "",
) -> None:
    """Fire-and-forget POST /api/chat (loopback interno)."""
    import urllib.request as _u
    slot = f"esteira-{repo.split('/')[-1]}-{issue['number']}"
    body = json.dumps({
        "message": _dispatch_prompt(repo, issue, cfg, prompt_extra=prompt_extra),
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

def _dry_run_report(
    scan_results: list,
    dispatch_devs: list,
    dispatch_reviewers: list,
    needs_human: list,
    blocked_bypass: list,
    rebranded: list,
    merge_prs: list,
    spec_invalid: list,
) -> None:
    """Imprime o relatório de dry-run no stdout sem executar nenhum efeito colateral."""

    print("[DRY-RUN] ──────────────────────────────────────────")
    print(f"[DRY-RUN] {len(scan_results)} issue(s) processada(s) pelo scan")
    print("[DRY-RUN] Decisões (nenhuma será executada):")
    print()

    for repo, issue, decision in dispatch_devs:
        print(f"[DRY-RUN] {repo}#{issue['number']} → DISPATCH_DEV (template via executor) — {issue['title']}")

    for repo, issue in dispatch_reviewers:
        print(f"[DRY-RUN] {repo}#{issue['number']} → DISPATCH_REVIEWER — {issue['title']}")

    for repo, issue in merge_prs:
        print(f"[DRY-RUN] {repo}#{issue['number']} → MERGE_PR — {issue['title']}")

    for result, decision, _sc in needs_human:
        r: ScanResult = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {r.item.key} → NOTIFY_HUMAN {getattr(getattr(decision, 'notify_role', None), 'value', '?')} ({r.item.title})")

    for result, decision in rebranded:
        r = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {r.item.key} → REBRAND → {getattr(decision, 'new_template', '?')} ({r.item.title})")

    for result in blocked_bypass:
        r = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {r.item.key} → BLOCK (hml-bypass sem justificativa) — {r.item.title}")

    for result in spec_invalid:
        r = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {r.item.key} → SPEC_INVALID (sem repo no título) — {r.item.title}")

    total_actions = (
        len(dispatch_devs) + len(dispatch_reviewers) + len(merge_prs)
        + len(needs_human) + len(rebranded) + len(blocked_bypass) + len(spec_invalid)
    )
    skipped = max(0, len(scan_results) - total_actions)
    if skipped > 0:
        print(f"[DRY-RUN] {skipped} issue(s) sem ação (SKIP)")

    print()
    print("[DRY-RUN] ── Nenhuma sessão despachada, label alterada ou notificação enviada. ──")


def run(ctx: object) -> None:
    cfg = _load_config()
    repos: list[str] = cfg.get("repos") or []
    auto = bool(cfg.get("auto_dispatch", False))
    max_conc = int(cfg.get("max_concurrent", 2))
    one_per_repo = bool(cfg.get("one_per_repo", True))
    chat_id = cfg.get("notify_chat_id") or ""
    issue_provider_name: str = cfg.get("issue_provider", "github")

    # ── Modo dry-run: inspeciona o que seria feito sem executar efeitos ──
    # Ativado por CREWFLOW_DRY_RUN=1 (variável de ambiente) ou dry_run: true na config.
    dry_run = bool(os.environ.get("CREWFLOW_DRY_RUN")) or bool(cfg.get("dry_run", False))
    if dry_run:
        logger.info("deployment: modo dry-run ativado — nenhuma ação será executada")

    if not repos:
        logger.warning("deployment: nenhum repo/projeto configurado")
        return

    # ── Carrega a SquadConfig: arquivo squads/<id>.yaml > inline da config ─
    from flow.config.squad import SquadConfig, SquadConfigError, load_squad

    squad: SquadConfig | None = None
    squad_file = cfg.get("squad_config")   # caminho opcional na config
    if squad_file and not os.path.exists(squad_file):
        raise RuntimeError(
            f"deployment: squad_config aponta para um arquivo que não existe: {squad_file!r}"
        )
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

    # Categorias de resultado após o executor — tipadas para mypy
    spec_invalid: list[ScanResult] = []
    dispatch_devs: list[tuple[str, dict, object]] = []   # (repo, issue, decision)
    dispatch_reviewers: list[tuple[str, dict]] = []      # (repo, issue)
    needs_human: list[tuple[ScanResult, object, str | None]] = []  # (result, decision, sc)
    blocked_bypass: list[ScanResult] = []                # result com bypass sem justif
    rebranded: list[tuple[ScanResult, object]] = []      # (result, decision)
    merge_prs: list[tuple[str, dict, str | None]] = []   # (repo, issue, state_comment) — merge squash automático

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
            or (result.current_state is State.TODO and "crewflow:debt" in result.item.labels)
            # GATE 2: lê o resultado do reviewer quando em review+reviewed
            or (result.current_state is State.REVIEW and Modifier.REVIEWED in result.modifiers)
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
            needs_human.append((result, decision, state_comment))
        elif decision.action is ActionKind.DISPATCH_REVIEWER:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            issue = _scan_result_to_issue(result)
            dispatch_reviewers.append((repo, issue))
        elif decision.action is ActionKind.MERGE_PR:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            issue = _scan_result_to_issue(result)
            merge_prs.append((repo, issue, state_comment))
        elif decision.action is ActionKind.DISPATCH_DEV:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            if not repo:
                repo = repos[0] if repos else ""
            issue = _scan_result_to_issue(result)
            dispatch_devs.append((repo, issue, decision))

    # Sem nada a fazer?
    if not any([spec_invalid, dispatch_devs, dispatch_reviewers,
                needs_human, blocked_bypass, rebranded, merge_prs]):
        return

    # ── Modo dry-run: imprime relatório e encerra sem executar ────────────
    if dry_run:
        _dry_run_report(
            scan_results=scan_results,
            dispatch_devs=dispatch_devs,
            dispatch_reviewers=dispatch_reviewers,
            needs_human=needs_human,
            blocked_bypass=blocked_bypass,
            rebranded=rebranded,
            merge_prs=merge_prs,
            spec_invalid=spec_invalid,
        )
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
        if _pr_exists(repo, issue["number"]):
            logger.info(
                "deployment: PR duplicado detectado para %s#%s — pulando dispatch",
                repo, issue["number"],
            )
            continue
        try:
            prompt_extra = squad.dispatch_prompt_extra if squad else ""
            _dispatch(ctx, repo, issue, cfg, prompt_extra=prompt_extra)
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
        for repo, issue in dispatch_reviewers:
            issue_number = issue["number"]
            if _reviewer_has_active(repo, issue_number):
                logger.info(
                    "deployment: reviewer já ativo para %s#%s — dispatch ignorado",
                    repo, issue_number,
                )
                continue
            try:
                _dispatch_reviewer(ctx, repo, issue, cfg)
            except Exception as exc:
                logger.error(
                    "deployment: erro ao despachar reviewer para %s#%s: %s",
                    repo, issue_number, exc,
                )

    if merge_prs:
        _execute_auto_merges(ctx, merge_prs, chat_id, provider)


def _notify_human_actions(ctx: object, items: list, chat_id: str) -> None:
    """Notifica o humano certo sobre ações pendentes.

    Anti-spam para gate-tl: só notifica se o state_comment não tiver
    status 'awaiting-tl-approval' — evita re-notificar a cada ciclo.
    Quando notifica, atualiza o comentário com esse status via GitHub API.
    """
    from flow.audit.state_comment import parse as _parse_sc
    from flow.audit.state_comment import render as _render_sc
    from flow.executor.executor import HumanRole
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    # Separa por papel; cada item é (ScanResult, ExecutorDecision, state_comment|None)
    tl_items = [
        (r, d, sc) for r, d, sc in items
        if getattr(d, "notify_role", None) is HumanRole.TL
    ]
    dev_items = [
        (r, d, sc) for r, d, sc in items
        if getattr(d, "notify_role", None) is HumanRole.DEV
    ]
    qa_items = [
        (r, d, sc) for r, d, sc in items
        if getattr(d, "notify_role", None) is HumanRole.QA
    ]

    # TL — notificação especial com instrução de aprovação + anti-spam
    tl_to_notify = []
    for r, d, sc in tl_items:
        # Anti-spam: se já estava aguardando aprovação, não re-notifica
        sc_obj = _parse_sc(sc) if sc else None
        already_waiting = sc_obj is not None and sc_obj.status == "awaiting-tl-approval"
        if already_waiting:
            logger.info(
                "deployment: NOTIFY_HUMAN TL já notificado anteriormente, pulando: %s",
                r.item.key,
            )
            continue
        tl_to_notify.append((r, d))

        # Se o state comment tem ReviewerResult, posta no PR antes de notificar o TL
        # (caso: reviewer retornou pedidos de mudança → NOTIFY_HUMAN TL)
        if sc_obj is not None and sc_obj.reviewer_result is not None:
            import contextlib
            with contextlib.suppress(Exception):
                key = r.item.key  # https://github.com/owner/repo/issues/N
                parts = key.rstrip("/").split("/")
                if len(parts) >= 5:
                    repo_path = f"{parts[-4]}/{parts[-3]}"
                    issue_num = int(parts[-1])
                    _post_reviewer_result_on_pr(repo_path, issue_num, key, sc)

        # Atualiza o status no comentário de estado para evitar re-notificação
        if sc_obj is not None:
            from datetime import datetime
            sc_obj.status = "awaiting-tl-approval"
            import contextlib
            with contextlib.suppress(Exception):
                from flow.adapters.github_client import upsert_state_comment
                # Extrai repo e número da issue da key
                key = r.item.key  # https://github.com/owner/repo/issues/N
                parts = key.rstrip("/").split("/")
                if len(parts) >= 5:
                    repo_path = f"{parts[-4]}/{parts[-3]}"
                    issue_num_str = parts[-1]
                    updated_body = _render_sc(sc_obj)
                    upsert_state_comment(repo_path, issue_num_str, updated_body)

    if tl_to_notify:
        from datetime import datetime
        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M")
        linhas_detail = []
        for r, d in tl_to_notify:
            issue_url = r.item.key
            issue_num = issue_url.rstrip("/").split("/")[-1]
            approval_block = (
                f"<!-- KIRO-FLOW-STATE -->\n"
                f"### Aprovações\n"
                f"| Gate | Aprovado por | Quando |\n"
                f"|------|-------------|--------|\n"
                f"| `gate-tl` | @seu-usuario | {now} |\n"
                f"<!-- /KIRO-FLOW-STATE -->"
            )
            linhas_detail.append(
                f"  📋 #{issue_num}: {r.item.title}\n"
                f"     Issue: {issue_url}\n"
                f"     Motivo: {d.reason}\n\n"
                f"     Para aprovar, comente na issue:\n"
                f"     {approval_block}"
            )
        msg = (
            f"🧾 KiroCrew Flow — GATE DT aguarda aprovação do TL{vm}\n\n"
            + "\n\n".join(linhas_detail)
        )
        ctx.notify(msg)  # type: ignore[attr-defined]

    if dev_items:
        linhas = "\n".join(
            f"  - {r.item.key}: {r.item.title} — {d.reason}"
            for r, d, _sc in dev_items
        )
        ctx.notify(f"KiroCrew Flow: aguarda ação do Dev.{vm}\n{linhas}")  # type: ignore[attr-defined]
    if qa_items:
        linhas = "\n".join(
            f"  - {r.item.key}: {r.item.title} — {d.reason}"
            for r, d, _sc in qa_items
        )
        ctx.notify(f"KiroCrew Flow: aguarda ação do QA.{vm}\n{linhas}")  # type: ignore[attr-defined]


def _reviewer_has_active(repo: str, issue_number: int) -> bool:
    """Retorna True se já existe sessão one-shot do reviewer ativa para esta issue.

    Análogo ao ``_repo_has_active``, mas escopado ao slot do reviewer:
    ``reviewer-<short_repo>-<issue_number>``.
    """
    short = repo.split("/")[-1]
    locks = glob.glob(
        os.path.join(_sessdir(), f"dashboard_reviewer-{short}-{issue_number}.jsonl.lock")
    )
    return any(not _lock_is_stale(p) for p in locks)


def _reviewer_prompt(repo: str, pr_number: int, issue_number: int) -> str:
    """Monta o prompt one-shot para a sessão do kiro-reviewer.

    A sessão deve:
    1. Ler a issue para contexto
    2. Ler o diff do PR
    3. Ler os steerings do repo
    4. Analisar e postar o resultado do review COMO COMENTÁRIO NO PR
    5. Registrar o ReviewerResult no state_comment DA ISSUE (o scan lê isso)
    6. Deixar uma referência curta na issue apontando pro PR + status
    7. Adicionar crewflow:reviewed se zero comentários
    8. ENCERRAR

    O formato do comentário do PR e da referência na issue tem UMA definição:
    os exemplares embutidos no prompt são produzidos pelos helpers puros
    ``render_pr_review_comment`` e ``render_issue_pr_reference`` de
    ``flow.audit.state_comment``. Assim o helper (exercitado por testes) e o
    prompt não divergem silenciosamente.
    """
    short = repo.split("/")[-1]

    # ── Fonte única de verdade para o formato do comentário ───────────────
    # Os exemplares abaixo são PRODUZIDOS pelos mesmos helpers puros de
    # flow/audit/state_comment.py que os testes exercitam. Assim o formato
    # tem UMA definição: se o helper mudar, o prompt muda junto (sem drift).
    from flow.audit.state_comment import ReviewerResult
    _rr_ok = ReviewerResult(approved=True, comments=(), sha="<sha>", reviewer="kiro-reviewer")
    _rr_ko = ReviewerResult(
        approved=False,
        comments=("<mudança 1>", "<mudança 2>"),
        sha="<sha>",
        reviewer="kiro-reviewer",
    )
    exemplo_aprovado = render_pr_review_comment(_rr_ok, issue_number=issue_number)
    exemplo_mudancas = render_pr_review_comment(_rr_ko, issue_number=issue_number)
    ref_issue = render_issue_pr_reference(pr_number, approved=True)

    def _indent(text: str, prefix: str = "     ") -> str:
        return "\n".join(prefix + line if line else line for line in text.splitlines())

    return (
        "------------ AGENT HEADER ----------------\n"
        f"REPO: {repo}\n"
        f"PR: #{pr_number}\n"
        f"ISSUE: #{issue_number}\n"
        f"SESSION TITLE: review: {short} PR #{pr_number} (issue #{issue_number})\n"
        "------------ CONTEXT TASK ----------------\n"
        "Você é um agente de code review ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.\n\n"
        "FLUXO (execute UMA vez, do início ao fim, e PARE):\n"
        "0. TÍTULO: como PRIMEIRA ação, defina o título da sessão = `SESSION TITLE`.\n"
        f"1. Leia a issue para ter contexto:\n"
        f"   gh issue view {issue_number} --repo {repo}\n"
        f"2. Leia o diff do PR:\n"
        f"   gh pr diff {pr_number} --repo {repo}\n"
        f"3. Leia os steerings do repo (.kiro/steering/*.md) para entender convenções.\n"
        "4. Analise: corretude, cobertura de testes, estilo, convenções do projeto.\n"
        "5. POSTE O RESULTADO DO REVIEW COMO COMENTÁRIO NO PR:\n"
        f"   gh pr comment {pr_number} --repo {repo} --body \"<corpo do review>\"\n"
        "   Use EXATAMENTE este formato no corpo (KiroCrew Review).\n"
        "   Se APROVADO sem comentários (omita a seção `### Pedidos de mudança`):\n"
        f"{_indent(exemplo_aprovado)}\n"
        "   Se houver pedidos de mudança:\n"
        f"{_indent(exemplo_mudancas)}\n"
        "6. Registre o resultado no state_comment DA ISSUE com ReviewerResult:\n"
        "   - Se APROVADO sem comentários: campo `approved: true`, `comments: []`\n"
        "   - Se tem pedidos de mudança: `approved: false`, `comments: [\"<mudança 1>\", ...]`\n"
        "   Use `upsert_state_comment` para atualizar o bloco <!-- KIRO-FLOW-STATE --> NA ISSUE.\n"
        "   O ReviewerResult deve incluir o SHA atual do HEAD do PR.\n"
        "   IMPORTANTE: o ReviewerResult PERMANECE na issue — é o que o scan lê pra decidir MERGE_PR.\n"
        f"7. Deixe uma referência CURTA na issue #{issue_number} apontando pro PR e o status:\n"
        f"   ex.: `{ref_issue}` (troque para `pedidos de mudança` se houver comentários).\n"
        "   NÃO duplique o detalhe dos pedidos de mudança na issue — só o link + status.\n"
        f"8. Se zero comentários: adicione a label `crewflow:reviewed` à issue #{issue_number}.\n"
        "9. Se tem comentários: NÃO adicione `crewflow:reviewed` — o TL decide.\n"
        "10. ENCERRE.\n\n"
        "REGRAS CRÍTICAS:\n"
        "- UMA passada. Terminou, acabou. NÃO entre em loop.\n"
        "- NUNCA mergeie. NUNCA faça deploy.\n"
        "- Seja objetivo — aponte problemas concretos, não estilo pessoal.\n"
        "------------------------------------------"
    )


def _dispatch_reviewer(
    ctx: object,
    repo: str,
    issue: dict,
    cfg: dict,
) -> None:
    """Fire-and-forget POST /api/chat para a sessão one-shot do kiro-reviewer.

    Localiza o PR aberto da issue e despacha o reviewer com o prompt correto.
    Fallback para notificação se o PR não for encontrado.
    """
    import subprocess

    issue_number = issue["number"]
    chat_id = cfg.get("notify_chat_id") or ""
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    # Localiza o PR aberto para a issue
    branch = f"feat/issue-{issue_number}"
    pr_number: int | None = None
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--head", branch,
             "--state", "open", "--json", "number"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout or "[]")
            if prs:
                pr_number = int(prs[0]["number"])
    except Exception as exc:
        logger.warning(
            "deployment: erro ao localizar PR para %s#%s no dispatch do reviewer: %s",
            repo, issue_number, exc,
        )

    if pr_number is None:
        # Fallback: não encontrou PR — apenas notifica
        logger.warning(
            "deployment: PR aberto não encontrado para %s#%s — notificando sem dispatch",
            repo, issue_number,
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: reviewer pendente — PR de {repo}#{issue_number} não localizado.{vm}"
        )
        return

    short = repo.split("/")[-1]
    slot = f"reviewer-{short}-{issue_number}"

    import urllib.request as _u
    body = json.dumps({
        "message": _reviewer_prompt(repo, pr_number, issue_number),
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
        logger.info(
            "deployment: reviewer one-shot despachado para %s PR #%s (issue #%s)",
            repo, pr_number, issue_number,
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: sessão one-shot do reviewer despachada — "
            f"{repo} PR #{pr_number} (issue #{issue_number}).{vm}"
        )
    except Exception as exc:
        logger.error(
            "deployment: falha ao despachar reviewer para %s#%s: %s",
            repo, issue_number, exc,
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: falha ao despachar reviewer para {repo}#{issue_number}.{vm}"
        )


def _post_reviewer_result_on_pr(
    repo: str,
    issue_number: int,
    issue_url: str,
    state_comment: str | None,
) -> None:
    """Posta (ou atualiza) o resultado do reviewer como comentário no PR.

    Localiza o PR aberto associado à issue, depois posta o comentário formatado.
    Silencioso em caso de erro — o resultado já está na issue; o PR é bonus.
    """
    import contextlib

    from flow.adapters import github_client as gh_client
    from flow.audit.state_comment import get_reviewer_result_from_comment, render_pr_review_comment

    reviewer_result = get_reviewer_result_from_comment(state_comment)
    if reviewer_result is None:
        logger.warning(
            "deployment: _post_reviewer_result_on_pr: ReviewerResult ausente para %s#%s",
            repo, issue_number,
        )
        return

    pr = gh_client.get_pr_for_issue(repo, issue_number)
    if pr is None:
        logger.info(
            "deployment: _post_reviewer_result_on_pr: PR aberto não encontrado para %s#%s — pulando",
            repo, issue_number,
        )
        return

    pr_number = pr["number"]
    body = render_pr_review_comment(
        reviewer_result,
        issue_number=issue_number,
        issue_url=issue_url,
    )

    with contextlib.suppress(Exception):
        gh_client.upsert_pr_review_comment(repo, pr_number, body)
        logger.info(
            "deployment: resultado do reviewer postado no PR #%s (%s#%s)",
            pr_number, repo, issue_number,
        )

    # Referência curta na issue: atualiza o state comment com link para o PR
    _update_issue_with_pr_ref(repo, issue_number, pr_number, reviewer_result)


def _update_issue_with_pr_ref(
    repo: str,
    issue_number: int,
    pr_number: int,
    reviewer_result: object,
) -> None:
    """Adiciona referência curta ao PR no comentário de estado da issue.

    Adiciona linha no histórico indicando onde o review foi postado.
    Não altera o ReviewerResult — o scan continua lendo dali.
    """
    import contextlib

    from flow.adapters import github_client as gh_client
    from flow.audit.state_comment import ReviewerResult
    from flow.audit.state_comment import parse as _parse
    from flow.audit.state_comment import render as _render

    rr: ReviewerResult = reviewer_result  # type: ignore[assignment]
    state_comment_body = gh_client.get_state_comment(repo, str(issue_number))
    if state_comment_body is None:
        return

    sc = _parse(state_comment_body)
    if sc is None:
        return

    status_str = "aprovado" if rr.approved else "pedidos de mudança"
    pr_url = f"https://github.com/{repo}/pull/{pr_number}"
    sc.add_transition(
        from_state="review",
        to_state=f"review (resultado no [PR #{pr_number}]({pr_url}))",
        actor="kiro-reviewer",
    )
    sc.status = f"review postado em PR #{pr_number} — {status_str}"

    with contextlib.suppress(Exception):
        gh_client.upsert_state_comment(repo, str(issue_number), _render(sc))


def _notify_reviewers(ctx: object, items: list, chat_id: str) -> None:
    """Notifica que o kiro-reviewer foi disparado (legado — mantido como referência)."""
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
    linhas = "\n".join(f"  - {repo}#{issue['number']}: {issue['title']}" for repo, issue in items)
    ctx.notify(  # type: ignore[attr-defined]
        f"KiroCrew Flow: análise automatizada de code review disparada.{vm}\n{linhas}"
    )


def _execute_auto_merges(
    ctx: object,
    items: list,
    chat_id: str,
    provider: object,
) -> None:
    """Executa merge squash automático para PRs aprovados sem comentários.

    Para cada (repo, issue, state_comment) em ``items``:
    1. Posta resultado do reviewer no PR (e referência curta na issue)
    2. Localiza o PR aberto associado à issue
    3. Faz o merge squash via GitHub API
    4. Atualiza labels: adiciona crewflow:done, remove crewflow:review e crewflow:reviewed
    5. Notifica TL com resultado (sucesso ou falha)
    """
    from flow.adapters import github_client as gh_client
    from flow.ports.issue_provider import ProviderError

    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    merged: list[tuple[str, dict]] = []
    failed: list[tuple[str, dict, str]] = []

    for repo, issue, state_comment in items:
        issue_number = issue["number"]
        issue_url = issue.get("url") or f"https://github.com/{repo}/issues/{issue_number}"
        try:
            # Posta resultado do reviewer no PR antes do merge
            _post_reviewer_result_on_pr(repo, issue_number, issue_url, state_comment)

            # Localiza o PR aberto associado à issue
            pr = gh_client.get_pr_for_issue(repo, issue_number)
            if pr is None:
                failed.append((repo, issue, f"PR aberto para #{issue_number} não encontrado"))
                continue

            pr_number = pr["number"]
            pr_branch = pr.get("headRefName") or pr.get("head", {}).get("ref", "")

            # Merge squash
            gh_client.merge_pull_request(repo, pr_number, merge_method="squash")

            # Deleta a branch após confirmar o merge
            if pr_branch:
                import contextlib
                with contextlib.suppress(Exception):
                    gh_client.delete_branch(repo, pr_branch)
                    logger.info(
                        "deployment: branch %s deletada após merge de #%s",
                        pr_branch, pr_number,
                    )

            # Atualiza labels da issue: remove review/reviewed, adiciona done
            try:
                item_data = gh_client.get_work_item(repo, str(issue_number))
                current_labels = list(item_data.get("labels", []))
                for lbl in ("crewflow:review", "crewflow:reviewed"):
                    if lbl in current_labels:
                        current_labels.remove(lbl)
                if "crewflow:done" not in current_labels:
                    current_labels.append("crewflow:done")
                gh_client.set_labels(repo, str(issue_number), current_labels)
            except Exception as exc:
                logger.warning(
                    "deployment: merge ok mas falha ao atualizar labels de %s#%s: %s",
                    repo, issue_number, exc,
                )

            # Comenta na issue registrando o merge automático
            from datetime import datetime
            now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
            import contextlib
            with contextlib.suppress(Exception):
                gh_client.add_issue_comment(
                    repo,
                    issue_number,
                    f"✅ **Merge automático** — PR #{pr_number} mergeado em {now}.\n\n"
                    f"Reviewer aprovou sem comentários. Branch `{pr_branch}` deletada.",
                )

            merged.append((repo, issue))
            logger.info(
                "deployment: merge squash automático PR #%s (issue #%s) em %s",
                pr_number, issue_number, repo,
            )

        except ProviderError as exc:
            msg = str(exc)
            failed.append((repo, issue, msg))
            logger.error(
                "deployment: falha no merge automático de %s#%s: %s",
                repo, issue_number, exc,
            )
        except Exception as exc:
            msg = str(exc)
            failed.append((repo, issue, msg))
            logger.error(
                "deployment: erro inesperado no merge de %s#%s: %s",
                repo, issue_number, exc,
            )

    if merged:
        linhas = "\n".join(
            f"  ✅ {r}#{i['number']}: {i['title']}"
            for r, i in merged
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: PR(s) mergeado(s) automaticamente — reviewer sem comentários.{vm}\n{linhas}"
        )

    if failed:
        linhas = "\n".join(
            f"  ❌ {r}#{i['number']}: {i['title']} — {motivo}"
            for r, i, motivo in failed
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: falha no merge automático.{vm}\n{linhas}"
        )
