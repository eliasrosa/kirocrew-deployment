"""Rotas do app kirocrew-flow registradas no gateway in-process.

Wiring correto (ver issue #170):

- As rotas HTTP são registradas pelo ``RouteRegistry`` do gateway a partir do
  hook declarado em ``app.json`` sob ``backend.hooks.routes`` (apontando para
  ``backend.routes:register_routes``). ``register_routes(ctx)`` retorna uma
  ``list[AppRoute]`` e o registry monta cada rota como
  ``/api/apps/kirocrew-flow{path}``.
- O ciclo de vida dos 4 loops asyncio de polling (dev/reviewer/merge/conflito)
  é de propriedade de ``backend/hooks.py`` (``on_startup``/``on_shutdown``),
  invocados pelo gateway como ``func(ctx)``.

Os helpers ``_start_loops(app)``/``_stop_loops(app)`` abaixo são o caminho
baseado em ``aiohttp.web.Application`` usado pelo servidor standalone e pelos
testes; o gateway NÃO os usa (ele entrega um ``AppContext``, não uma
``Application``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from datetime import UTC
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)


async def _start_loops(app: web.Application) -> None:
    """Inicia o loop asyncio de polling do single-flow (caminho aiohttp/standalone).

    Usado pelo servidor standalone e pelos testes via ``app.on_startup``. O
    gateway usa o caminho baseado em ``ctx`` em ``backend/hooks.py:on_startup``.
    """
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    try:
        from backend.server import _single_flow_loop

        app["crewflow_tasks"] = [
            asyncio.create_task(
                _single_flow_loop(
                    int(os.environ.get("CREWFLOW_SINGLE_FLOW_INTERVAL", "60")),
                )
            ),
        ]
        print("[kirocrew-flow] on_startup: single-flow loop iniciado", flush=True)
    except Exception as exc:
        print(f"[kirocrew-flow] on_startup error: {exc}", flush=True)


async def _stop_loops(app: web.Application) -> None:
    """Cancela os loops asyncio e limpa a lista (caminho aiohttp/standalone).

    Usado pelo servidor standalone e pelos testes via ``app.on_cleanup``. O
    gateway usa o caminho baseado em ``ctx`` em ``backend/hooks.py:on_shutdown``.
    """
    for task in app.get("crewflow_tasks", []):
        task.cancel()
    app["crewflow_tasks"] = []
    print("[kirocrew-flow] on_cleanup: loops cancelados", flush=True)


def register_routes(ctx: object) -> list:
    """Registra as rotas do app kirocrew-flow no RouteRegistry do gateway.

    Assinatura correta para apps de terceiros (não builtins):
    - Recebe ``ctx: AppContext`` (duck-typed como object)
    - Retorna ``list[AppRoute]`` — o registry monta as rotas como
      ``/api/apps/{app_name}{path}`` automaticamente

    O gateway chega até esta função pelo hook declarado em ``app.json`` sob
    ``backend.hooks.routes`` (``on_app_enable`` lê ``backend.hooks.routes``,
    não ``backend.routes`` — ver issue #170). Diferente dos apps builtins, que
    usam ``app.router.add_get(...)`` diretamente, apps de terceiros usam o
    RouteRegistry via esta interface. O ciclo de vida dos loops de polling é de
    ``backend/hooks.py`` (``on_startup``/``on_shutdown``), não daqui.
    Descoberto via inspeção de ``kiro_crew.apps.route_registry.py``.
    """
    try:
        from kiro_crew.apps.route_registry import AppRoute  # type: ignore[import]
    except ImportError:
        # Fallback: criar AppRoute simples se não disponível (desenvolvimento local)
        from dataclasses import dataclass
        from typing import Any

        @dataclass
        class AppRoute:  # type: ignore[no-redef]
            method: str
            path: str
            handler: Any

    return [
        AppRoute("GET", "/health", handle_health),
        AppRoute("GET", "/issues", handle_issues),
        AppRoute("POST", "/dispatch", handle_dispatch),
        AppRoute("POST", "/claim", handle_claim),
        AppRoute("POST", "/advance", handle_advance),
        AppRoute("POST", "/qa-fail", handle_qa_fail),
        AppRoute("POST", "/qa-approve", handle_qa_approve),
        AppRoute("POST", "/gate/{token}/decide", handle_gate_decide),
        AppRoute("GET",  "/gate/{token}", handle_gate_get),
    ]


async def handle_health(request: web.Request, ctx: object = None) -> web.Response:
    try:
        from backend.version import get_version
    except ImportError:
        # Fallback para quando o módulo roda no namespace isolado do gateway
        import importlib.util as _ilu
        _vpath = Path(__file__).parent / "version.py"
        _vspec = _ilu.spec_from_file_location("_kf_version", str(_vpath))
        _vmod = _ilu.module_from_spec(_vspec)  # type: ignore[arg-type]
        _vspec.loader.exec_module(_vmod)  # type: ignore[union-attr]
        get_version = _vmod.get_version

    return web.json_response({"ok": True, "app": "kirocrew-flow", "version": get_version()})


async def handle_claim(request: web.Request, ctx: object = None) -> web.Response:
    """Registra uma task no ledger do single-flow (sem chamar provider externo).

    Body JSON: {"task_key": "VGAT-1009"} ou {"task_key": "owner/repo#42"}
    Registra a task no estágio de entrada (flow:briefing) se ainda não estiver ativa.
    Retorna {"ok": true, "claimed": true|false, "task_key": "...", "stage": "..."}.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "body JSON inválido"}, status=400)

    task_key = (body.get("task_key") or "").strip()
    if not task_key:
        return web.json_response(
            {"ok": False, "error": "campo 'task_key' é obrigatório"},
            status=400,
        )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_claim, task_key)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("handle_claim: erro inesperado: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


def _do_claim(task_key: str) -> dict:
    """Registra task_key no ledger sem chamar provider externo."""
    # O gateway carrega o módulo via namespace isolado sem adicionar app_dir ao
    # sys.path. Precisamos garantir que tanto o app instalado quanto o repo
    # estejam no path para que `flow` e `deployment` sejam importáveis.
    app_root = Path(__file__).parent.parent
    _installed_root = os.path.expanduser("~/.kiro/crew/apps/kirocrew-flow")
    for _p in (_installed_root, str(app_root)):
        if _p not in sys.path:
            sys.path.insert(0, _p)

    import importlib.util
    import re

    from flow.domain.run_ledger import SqliteRunLedger

    # Carrega deployment.py instalado (mesmo padrão do _force_dispatch)
    _crons_dir = os.path.expanduser("~/.kiro/crew/crons")
    _deploy_py = os.path.join(_crons_dir, "deployment.py")
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_deploy_claim", _deploy_py)
    _mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    _load_config = _mod._load_config
    _resolve_squad_id = _mod._resolve_squad_id

    cfg = _load_config()
    squad_id = _resolve_squad_id(cfg)

    # Deriva o repo do task_key (VGAT-1009 → repo="VGAT"; owner/repo#42 → repo="owner/repo")
    m_jira = re.match(r"^([A-Z][A-Z0-9]+)-(\d+)$", task_key)
    if m_jira:
        repo = m_jira.group(1)
    else:
        m_gh = re.match(r"([^#]+)#\d+$", task_key)
        repo = m_gh.group(1) if m_gh else task_key

    entry_stage = "flow:briefing"
    ledger = SqliteRunLedger(squad_id)
    try:
        claimed = ledger.claim(task_key, repo, entry_stage)
        active = ledger.active()
        current_stage = active.current_stage if active else entry_stage
    finally:
        ledger.close()

    return {
        "ok": True,
        "claimed": claimed,
        "task_key": task_key,
        "repo": repo,
        "stage": current_stage,
        "note": "já estava ativa" if not claimed else "registrada em flow:briefing",
    }


async def handle_advance(request: web.Request, ctx: object = None) -> web.Response:
    """Avança manualmente o estágio de uma task no ledger (sem checar provider externo).

    Body JSON: {"task_key": "VGAT-1009"}
    Força a transição para o próximo estágio no _NEXT_STATE do motor.
    Se o estágio resultante for ativo (briefing/planning-specs), dispara o tick.
    Retorna {"ok": true, "from": "...", "to": "...", "dispatched": true|false}.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "body JSON inválido"}, status=400)

    task_key = (body.get("task_key") or "").strip()
    if not task_key:
        return web.json_response(
            {"ok": False, "error": "campo 'task_key' é obrigatório"},
            status=400,
        )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _do_advance, task_key)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("handle_advance: erro inesperado: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


def _do_advance(task_key: str) -> dict:
    """Força o próximo estágio no ledger e dispara tick se for estágio ativo."""
    app_root = Path(__file__).parent.parent
    _installed_root = os.path.expanduser("~/.kiro/crew/apps/kirocrew-flow")
    for _p in (_installed_root, str(app_root)):
        if _p not in sys.path:
            sys.path.insert(0, _p)

    import importlib.util

    from flow.domain.run_ledger import SqliteRunLedger
    from flow.engine.ledger_tick import _ACTIVE_STAGES, _NEXT_STATE, State

    # Carrega deployment.py instalado (mesmo padrão do _force_dispatch)
    _crons_dir = os.path.expanduser("~/.kiro/crew/crons")
    _deploy_py = os.path.join(_crons_dir, "deployment.py")
    _spec = importlib.util.spec_from_file_location("_kirocrew_flow_deploy_advance", _deploy_py)
    _mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    _load_config = _mod._load_config
    _resolve_squad_id = _mod._resolve_squad_id
    run_single_flow = _mod.run_single_flow

    cfg = _load_config()
    squad_id = _resolve_squad_id(cfg)
    ledger = SqliteRunLedger(squad_id)
    try:
        active = ledger.active()
        if active is None or active.task_key != task_key:
            return {"ok": False, "error": f"task {task_key!r} não está ativa no ledger"}

        current = State(active.current_stage)
        nxt = _NEXT_STATE.get(current)
        if nxt is None:
            return {
                "ok": False,
                "error": f"estágio {current.value!r} não tem próximo (terminal ou desconhecido)",
            }

        ledger.advance(task_key, nxt.value, stage_session=None)
        from_stage = current.value
        to_stage = nxt.value
    finally:
        ledger.close()

    # Se o novo estágio é ativo (briefing/planning-specs), dispara tick imediato
    dispatched = False
    if nxt in _ACTIVE_STAGES:
        try:
            try:
                from backend.ctx import BackendCronCtx
            except ImportError:
                import importlib.util as _ilu2
                _cpath = Path(__file__).parent / "ctx.py"
                _cspec = _ilu2.spec_from_file_location("_kf_ctx", str(_cpath))
                _cmod = _ilu2.module_from_spec(_cspec)  # type: ignore[arg-type]
                _cspec.loader.exec_module(_cmod)  # type: ignore[union-attr]
                BackendCronCtx = _cmod.BackendCronCtx
            ctx_cron = BackendCronCtx(message=task_key)
            run_single_flow(ctx_cron)
            dispatched = True
        except Exception as exc:
            logger.warning("_do_advance: run_single_flow falhou: %s", exc)

    return {
        "ok": True,
        "task_key": task_key,
        "from": from_stage,
        "to": to_stage,
        "dispatched": dispatched,
        "note": "tick disparado" if dispatched else "estágio avançado (sem dispatch automático neste estágio)",
    }


def _extract_issue_number(raw: dict) -> int | str:
    """Extrai o número inteiro da issue do objeto raw do scan.

    O scan do engine usa ``key`` como URL completa
    (``https://github.com/owner/repo/issues/N``). O campo ``number`` pode ser
    None, ausente, um int, ou a própria URL — dependendo da versão do adapter.
    """
    num = raw.get("number")
    if isinstance(num, int):
        return num
    # Tentar extrair da URL em qualquer campo relevante
    for val in (num, raw.get("key", ""), raw.get("url", "")):
        if isinstance(val, str) and "/issues/" in val:
            try:
                return int(val.rstrip("/").split("/issues/")[-1])
            except ValueError:
                pass
    return num or ""


def _load_issues_from_github() -> dict[str, object]:
    """Carrega issues agrupadas por estágio crewflow:* via scan (zero-token, cache-first).

    Usa o engine existente (flow/adapters + flow/scan) — sem gastar token.
    Retorna um dict com squad_name, project e columns (colunas do kanban).
    """
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    import time

    from flow.config.squad import load_squads_dir
    from flow.domain.state import Modifier, parse_modifiers, parse_state
    from flow.ports.issue_provider import provider_for
    from flow.scan.cache import open_cache
    from flow.scan.scanner import SquadScanConfig

    # Descobre o diretório de squads relativo ao app root
    squads_dir = app_root / "squads"
    if not squads_dir.exists():
        logger.warning("handle_issues: squads/ não encontrado em %s", app_root)
        return {"squad_name": "KiroCrew Flow", "project": "", "columns": _empty_columns()}

    try:
        squads = load_squads_dir(squads_dir)
    except Exception as exc:
        logger.warning("handle_issues: falha ao carregar squads: %s", exc)
        return {"squad_name": "KiroCrew Flow", "project": "", "columns": _empty_columns()}

    if not squads:
        logger.warning("handle_issues: nenhuma squad configurada em %s", squads_dir)
        return {"squad_name": "KiroCrew Flow", "project": "", "columns": _empty_columns()}

    # Pega o nome da primeira squad para exibir no título
    first_squad = squads[0]
    squad_name: str = getattr(first_squad, "name", "") or "KiroCrew Flow"
    primary_project: str = first_squad.projects[0] if first_squad.projects else ""

    # Agrupa por estágio (todos os results de todas as squads)
    columns: dict[str, list[dict]] = _empty_columns()
    now = time.time()

    for squad in squads:
        provider = provider_for(squad.issue_provider)
        scan_cfg = SquadScanConfig(
            squad_id=squad.id,
            issue_provider=squad.issue_provider,
            projects=tuple(squad.projects),
            repos=squad.repos,
        )

        try:
            conn = open_cache(scan_cfg.squad_id)
            # Também incluir TODOS os estados no scan, não só candidatos a dispatch
            # Para isso varremos direto o provider por cada estado
            all_items = _fetch_all_state_items(squad.projects, provider)
            conn.close()
        except Exception as exc:
            logger.warning("handle_issues: erro no scan da squad %s: %s", squad.id, exc)
            continue

        for raw in all_items:
            labels = set(raw.get("labels", []))
            try:
                state = parse_state(labels)
            except Exception:
                continue
            if state is None:
                continue

            # Mapeia State → nome da coluna do kanban
            col = _state_to_column(state)
            if col is None:
                continue

            modifiers = parse_modifiers(labels)

            # Calcula age_min: tempo desde a criação ou updated_at
            created_at = raw.get("created_at") or ""
            age_min = _age_minutes(created_at, now)

            issue_entry = {
                "number": _extract_issue_number(raw),
                "title": raw.get("title", ""),
                "repo": raw.get("repo", "") or _repo_from_key(raw.get("key", "")),
                "url": raw.get("url", ""),
                "age_min": age_min,
                "labels": sorted(labels),
                "blocked": Modifier.BLOCKED in modifiers,
                "running": "flow:develop-running" in labels,
                "implicit_state": _derive_implicit_state(raw, raw.get("repo", "") or _repo_from_key(raw.get("key", ""))),
            }

            # Issues com flow:blocked vão para a coluna "blocked" (separada)
            if Modifier.BLOCKED in modifiers:
                columns["blocked"].append(issue_entry)
            else:
                columns[col].append(issue_entry)

    return {"squad_name": squad_name, "project": primary_project, "columns": columns}


def _fetch_all_state_items(projects: list[str], provider: object) -> list[dict]:
    """Busca todas as issues com labels crewflow:* de estado em todos os projetos."""
    from flow.domain.state import State
    from flow.ports.issue_provider import ProviderError

    all_items: list[dict] = []
    seen_keys: set[str] = set()

    for project in projects:
        for state in State:
            try:
                items = provider.list_by_state(project, state.value)  # type: ignore[attr-defined]
                for item in items:
                    key = item.get("key", "") or str(item.get("number", ""))
                    if key and key not in seen_keys:
                        seen_keys.add(key)
                        # Adiciona o repo ao item se não tiver
                        if not item.get("repo"):
                            enriched = dict(item)
                            enriched["repo"] = project
                        else:
                            enriched = item
                        all_items.append(enriched)
            except ProviderError as exc:
                logger.warning(
                    "handle_issues: erro ao listar %s em %s: %s", state.value, project, exc
                )

    return all_items


def _state_to_column(state: object) -> str | None:
    """Mapeia um State para o nome da coluna do Kanban."""
    from flow.domain.state import State

    mapping: dict[State, str | None] = {
        State.BRIEFING:        "briefing",
        State.PLANNING_SPECS:  "planning_specs",
        State.PLANNING_REVIEW: "planning_review",
        State.DEVELOP_WAITING: "develop_waiting",
        State.DEVELOP_RUNNING: "develop_running",
        State.REVIEW_WAITING:  "review_waiting",
        State.REVIEW_APPROVED: "review_approved",
        State.REVIEW_REFUSED:  "review_refused",
        State.QA_WAITING:      "qa_waiting",
        State.QA_TESTING:      "qa_testing",
        State.QA_APPROVED:     "qa_approved",
        State.QA_REFUSED:      "qa_refused",
        State.DONE:            "done",
    }
    if not isinstance(state, State):
        return None
    return mapping.get(state)


def _empty_columns() -> dict[str, list[dict]]:
    """Retorna as colunas vazias do kanban."""
    return {
        "briefing": [],
        "planning_specs": [],
        "planning_review": [],
        "develop_waiting": [],
        "develop_running": [],
        "review_waiting": [],
        "review_approved": [],
        "review_refused": [],
        "qa_waiting": [],
        "qa_testing": [],
        "qa_approved": [],
        "qa_refused": [],
        "done": [],
        "blocked": [],
    }


def _age_minutes(created_at: str, now: float) -> int:
    """Calcula a idade em minutos a partir de um timestamp ISO 8601."""
    if not created_at:
        return 0
    try:
        import datetime

        dt = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        elapsed = now - dt.timestamp()
        return max(0, int(elapsed / 60))
    except Exception:
        return 0


def _repo_from_key(key: str) -> str:
    """Extrai o repo de uma key no formato 'owner/repo#N'."""
    if "#" in key:
        return key.split("#")[0]
    return ""


def _derive_implicit_state(raw: dict, project: str) -> str | None:
    """Deriva o estado implícito de uma issue a partir de evidências externas.

    Usa branch + PR + estado da issue para calcular ``ImplicitState``.
    Retorna o valor string do estado (ex: ``"dev"``) ou ``None`` se não for
    possível derivar (erro ou provedor não suportado).

    Fail-safe: qualquer exceção retorna None — não bloqueia a resposta do /issues.
    """
    try:
        import re as _re

        from flow.adapters import github_client as _gh
        from flow.scan.scanner import implicit_state as _implicit_state

        key = raw.get("key", "") or raw.get("url", "")
        m = _re.search(r"[#\-/](\d+)$", key)
        if not m:
            return None
        issue_number = int(m.group(1))

        # Estado fechado
        issue_closed = (raw.get("state") or "").lower() == "closed"

        # Verifica branch canônica
        branch_name = f"feat/issue-{issue_number}"
        branches: list[str] = []
        if project and _gh.get_branch_exists(project, branch_name):
            branches = [branch_name]

        # Busca PR aberta e reviews
        prs: list[dict] = []
        if project:
            pr = _gh.get_pr_for_issue(project, issue_number)
            if pr:
                pr_number = pr.get("number")
                reviews: list[dict] = []
                if pr_number:
                    with contextlib.suppress(Exception):
                        reviews = _gh.get_pr_reviews(project, int(pr_number))
                pr_entry = dict(pr)
                pr_entry["state"] = "open"
                pr_entry["reviews"] = reviews
                prs = [pr_entry]

        result = _implicit_state(
            issue_closed=issue_closed,
            branches=branches,
            prs=prs,
            issue_number=issue_number,
        )
        return result.value
    except Exception:
        return None


async def handle_issues(request: web.Request, ctx: object = None) -> web.Response:
    """Lista issues por estágio (crewflow:*). Zero-token, cache-first."""
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _load_issues_from_github)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("handle_issues: erro inesperado: %s", exc)
        return web.json_response(
            {"error": str(exc), "squad_name": "KiroCrew Flow", "project": "", "columns": _empty_columns()},
            status=500,
        )


async def handle_dispatch(request: web.Request, ctx: object = None) -> web.Response:
    """Force dispatch manual de uma issue.

    Body JSON: {"repo": "owner/repo", "number": 123}
    Marca a issue crewflow:todo (se ainda não estiver) e dispara o estágio dev.
    Retorna {"ok": true, "dispatched": true}.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "error": "body JSON inválido"},
            status=400,
        )

    repo = body.get("repo", "")
    number = body.get("number")

    if not repo or not number:
        return web.json_response(
            {"ok": False, "error": "campos 'repo' e 'number' são obrigatórios"},
            status=400,
        )

    try:
        number = int(number)
    except (TypeError, ValueError):
        return web.json_response(
            {"ok": False, "error": "'number' deve ser um inteiro"},
            status=400,
        )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _force_dispatch, repo, number)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("handle_dispatch: erro inesperado: %s", exc)
        return web.json_response(
            {"ok": False, "error": str(exc)},
            status=500,
        )


def _force_dispatch(repo: str, issue_number: int) -> dict:
    """Força o dispatch de uma issue via single-flow.

    Registra a issue no ledger (claim_single_flow) e dispara um tick imediato.
    """
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    from backend.ctx import BackendCronCtx
    from deployment.deployment import claim_single_flow, run_single_flow

    ctx = BackendCronCtx()

    # Registra a task no ledger (se ainda não estiver)
    try:
        task_key = f"{repo}#{issue_number}"
        result = claim_single_flow(ctx)
        logger.info("force_dispatch: claim_single_flow → %s", result)
    except Exception as exc:
        logger.warning("force_dispatch: claim_single_flow falhou para %s: %s", task_key, exc)

    # Dispara um tick imediato
    try:
        run_single_flow(ctx)
        logger.info("force_dispatch: run_single_flow executado para %s#%s", repo, issue_number)
    except Exception as exc:
        logger.warning(
            "force_dispatch: run_single_flow falhou para %s#%s: %s",
            repo, issue_number, exc,
        )
        return {
            "ok": True,
            "dispatched": False,
            "note": f"claim ok, mas dispatch falhou: {exc}",
        }

    return {"ok": True, "dispatched": True}


async def handle_qa_fail(request: web.Request, ctx: object = None) -> web.Response:
    """Reprova uma issue no QA: adiciona flow:qa-refused e remove flow:qa-testing.

    Body JSON: {"repo": "owner/repo", "number": 123, "reason": "motivo opcional"}

    O estado flow:qa-refused é um gate humano — o TL/dev decide o próximo passo
    manualmente (mover para flow:develop-waiting ou flow:develop-running).
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "body JSON inválido"}, status=400)

    repo = body.get("repo", "")
    number = body.get("number")
    reason = body.get("reason", "")

    if not repo or not number:
        return web.json_response(
            {"ok": False, "error": "campos 'repo' e 'number' são obrigatórios"},
            status=400,
        )

    try:
        number = int(number)
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "'number' deve ser um inteiro"}, status=400)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _mark_qa_fail, repo, number, reason)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("handle_qa_fail: erro inesperado: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_qa_approve(request: web.Request, ctx: object = None) -> web.Response:
    """Aprova uma issue no QA: move de flow:qa-testing para flow:qa-approved.

    Body JSON: {"repo": "owner/repo", "number": 123}

    O executor detecta flow:qa-approved e faz o merge final automaticamente.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "body JSON inválido"}, status=400)

    repo = body.get("repo", "")
    number = body.get("number")

    if not repo or not number:
        return web.json_response(
            {"ok": False, "error": "campos 'repo' e 'number' são obrigatórios"},
            status=400,
        )

    try:
        number = int(number)
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "'number' deve ser um inteiro"}, status=400)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _mark_qa_approve, repo, number)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("handle_qa_approve: erro inesperado: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


def _mark_qa_fail(repo: str, issue_number: int, reason: str) -> dict:
    """Marca flow:qa-refused na issue (gate humano — sem dispatch automático).

    Remove flow:qa-testing e adiciona flow:qa-refused.
    Posta comentário com o motivo para o TL/dev lerem antes de decidir.
    """
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    from flow.adapters import github_client as gh
    from flow.domain.state import State, parse_state
    from flow.ports.issue_provider import ProviderError, ProviderNotFoundError

    try:
        item = gh.get_work_item(repo, str(issue_number))
    except ProviderNotFoundError:
        return {"ok": False, "error": f"issue #{issue_number} não encontrada em {repo!r}"}
    except ProviderError as exc:
        return {"ok": False, "error": f"erro ao acessar a issue: {exc}"}

    current_labels = set(item.get("labels", []))
    state = parse_state(current_labels)

    if state not in (State.QA_TESTING, State.QA_WAITING):
        return {
            "ok": False,
            "error": f"issue #{issue_number} não está em qa-testing/qa-waiting (estado atual: {state})",
        }

    # Troca qa-testing → qa-refused
    new_labels = (current_labels - {"flow:qa-testing", "flow:qa-waiting"}) | {"flow:qa-refused"}
    try:
        gh.set_labels(repo, str(issue_number), sorted(new_labels))
    except ProviderError as exc:
        return {"ok": False, "error": f"erro ao aplicar flow:qa-refused: {exc}"}

    # Posta comentário com o motivo
    if reason:
        import contextlib
        with contextlib.suppress(Exception):
            gh.add_issue_comment(
                repo,
                issue_number,
                f"❌ **QA Reprovado** — motivo: {reason}\n\n"
                f"A issue está em `flow:qa-refused` (gate humano). "
                f"TL/dev deve avaliar e mover manualmente para `flow:develop-waiting` "
                f"(novo ciclo) ou `flow:develop-running` (rework direto).",
            )

    return {"ok": True, "qa_refused": True}


def _mark_qa_approve(repo: str, issue_number: int) -> dict:
    """Aprova a issue no QA: move de flow:qa-testing para flow:qa-approved.

    O executor detecta flow:qa-approved e faz o merge squash final automaticamente.
    """
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    from flow.adapters import github_client as gh
    from flow.domain.state import State, parse_state
    from flow.ports.issue_provider import ProviderError, ProviderNotFoundError

    try:
        item = gh.get_work_item(repo, str(issue_number))
    except ProviderNotFoundError:
        return {"ok": False, "error": f"issue #{issue_number} não encontrada em {repo!r}"}
    except ProviderError as exc:
        return {"ok": False, "error": f"erro ao acessar a issue: {exc}"}

    current_labels = set(item.get("labels", []))
    state = parse_state(current_labels)

    if state not in (State.QA_TESTING, State.QA_WAITING):
        return {
            "ok": False,
            "error": f"issue #{issue_number} não está em qa-testing/qa-waiting (estado atual: {state})",
        }

    # Troca qa-testing → qa-approved (o executor faz o merge squash final)
    new_labels = (current_labels - {"flow:qa-testing", "flow:qa-waiting"}) | {"flow:qa-approved"}
    try:
        gh.set_labels(repo, str(issue_number), sorted(new_labels))
    except ProviderError as exc:
        return {"ok": False, "error": f"erro ao aplicar flow:qa-approved: {exc}"}

    return {"ok": True, "qa_approved": True}


# ── Gate de aprovação humana (workflow engine) ──────────────────────────────

async def handle_gate_get(request: web.Request, ctx: object = None) -> web.Response:
    """GET /gate/{token} -- Retorna o estado atual de um gate de aprovação.

    Usado pela UI para renderizar o card do gate (prompt + opções + estado).
    Resposta:
        {"token": "...", "run_id": "...", "node_id": "...", "prompt": "...",
         "options": [...], "decision": null|"approve"|..., "expires_at": "..."}
    """
    token = request.match_info.get("token", "")
    if not token:
        return web.json_response({"ok": False, "error": "token ausente"}, status=400)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _gate_get, token)
        if result is None:
            return web.json_response({"ok": False, "error": "token não encontrado"}, status=404)
        return web.json_response(result)
    except Exception as exc:
        logger.exception("handle_gate_get: erro: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_gate_decide(request: web.Request, ctx: object = None) -> web.Response:
    """POST /gate/{token}/decide -- Registra a decisão humana num gate.

    Body JSON: {"decision": "approve"} (ou qualquer opção declarada no YAML).
    Idempotente: um segundo POST com a mesma decisão retorna ok=true sem erro.
    Um segundo POST com decisão diferente retorna ok=false (já decidido).

    Resposta sucesso:  {"ok": true,  "token": "...", "decision": "approve"}
    Resposta conflito: {"ok": false, "error": "already_decided", "decision": "<decisão anterior>"}
    Resposta expirado: {"ok": false, "error": "token_expired"}
    """
    token = request.match_info.get("token", "")
    if not token:
        return web.json_response({"ok": False, "error": "token ausente"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "body JSON inválido"}, status=400)

    decision = body.get("decision", "").strip()
    if not decision:
        return web.json_response(
            {"ok": False, "error": "campo 'decision' é obrigatório"},
            status=400,
        )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _gate_decide, token, decision)
        status = 200 if result.get("ok") else 409
        return web.json_response(result, status=status)
    except Exception as exc:
        logger.exception("handle_gate_decide: erro: %s", exc)
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


def _gate_get(token: str) -> dict | None:
    """Lê o estado de um gate token no SQLite do engine."""
    import sys
    from pathlib import Path

    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    from backend.engine import db

    # Busca pelo token (PK da tabela)
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_gate_tokens WHERE token = ?",
            (token,),
        ).fetchone()

    if row is None:
        return None

    import json as _json
    data = dict(row)
    data["options"] = _json.loads(data.get("options_json") or "[]")
    data.pop("options_json", None)
    return data


def _gate_decide(token: str, decision: str) -> dict:
    """Registra a decisão humana num gate. Validações:
    - Token inexistente → erro not_found
    - Token expirado    → erro token_expired
    - Já decidido com a mesma opção → ok=true (idempotente)
    - Já decidido com outra opção   → erro already_decided
    - Decisão não está nas opções   → erro invalid_option
    """
    import json as _json
    import sys
    from datetime import datetime
    from pathlib import Path

    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    from backend.engine import db

    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM workflow_gate_tokens WHERE token = ?",
            (token,),
        ).fetchone()

    if row is None:
        return {"ok": False, "error": "not_found"}

    data = dict(row)
    options = _json.loads(data.get("options_json") or "[]")

    # Verifica expiração antes de qualquer coisa
    expires_at = data.get("expires_at", "")
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if datetime.now(UTC) > exp:
                return {"ok": False, "error": "token_expired", "token": token}
        except ValueError:
            pass

    # Verifica se a decisão é uma opção válida
    if options and decision not in options:
        return {
            "ok": False,
            "error": "invalid_option",
            "valid_options": options,
        }

    existing_decision = data.get("decision")

    # Já decidido
    if existing_decision is not None:
        if existing_decision == decision:
            # Idempotente -- mesma decisão
            return {"ok": True, "token": token, "decision": decision, "idempotent": True}
        return {
            "ok": False,
            "error": "already_decided",
            "decision": existing_decision,
        }

    # Grava a decisão
    accepted = db.decide_gate_token(token, decision)
    if not accepted:
        # Race condition improvável -- token foi decidido entre o SELECT e o UPDATE
        return {"ok": False, "error": "already_decided"}

    logger.info("gate_decide: token=%s decision=%s", token, decision)
    return {"ok": True, "token": token, "decision": decision}
