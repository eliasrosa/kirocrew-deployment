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
import logging
import os
import sys
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)


async def _start_loops(app: web.Application) -> None:
    """Inicia os 4 loops asyncio de polling da esteira (caminho aiohttp/standalone).

    Usado pelo servidor standalone e pelos testes via ``app.on_startup``. O
    gateway usa o caminho baseado em ``ctx`` em ``backend/hooks.py:on_startup``.
    """
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    try:
        from backend.server import _run_stage_loop
        from deployment.deployment import (
            _STAGE_CONFLITO,
            _STAGE_DEV,
            _STAGE_MERGE,
            _STAGE_REVIEWER,
        )

        app["crewflow_tasks"] = [
            asyncio.create_task(
                _run_stage_loop(
                    _STAGE_DEV,
                    int(os.environ.get("CREWFLOW_DEV_INTERVAL", "300")),
                )
            ),
            asyncio.create_task(
                _run_stage_loop(
                    _STAGE_REVIEWER,
                    int(os.environ.get("CREWFLOW_REVIEWER_INTERVAL", "180")),
                )
            ),
            asyncio.create_task(
                _run_stage_loop(
                    _STAGE_MERGE,
                    int(os.environ.get("CREWFLOW_MERGE_INTERVAL", "120")),
                )
            ),
            asyncio.create_task(
                _run_stage_loop(
                    _STAGE_CONFLITO,
                    int(os.environ.get("CREWFLOW_CONFLITO_INTERVAL", "300")),
                )
            ),
        ]
        print("[kirocrew-flow] on_startup: 4 loops asyncio iniciados", flush=True)
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
    ]


async def handle_health(request: web.Request, ctx: object = None) -> web.Response:
    return web.json_response({"ok": True, "app": "kirocrew-flow", "version": "1.0.0"})


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


def _load_issues_from_github() -> dict[str, list[dict]]:
    """Carrega issues agrupadas por estágio crewflow:* via scan (zero-token, cache-first).

    Usa o engine existente (flow/adapters + flow/scan) — sem gastar token.
    Retorna um dict com chave = nome do estágio (ex: "todo") e valor = lista de issues.
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
        return _empty_columns()

    try:
        squads = load_squads_dir(squads_dir)
    except Exception as exc:
        logger.warning("handle_issues: falha ao carregar squads: %s", exc)
        return _empty_columns()

    if not squads:
        logger.warning("handle_issues: nenhuma squad configurada em %s", squads_dir)
        return _empty_columns()

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
                "running": Modifier.RUNNING in modifiers,
            }

            # Issues com crewflow:blocked vão para a coluna "blocked" (separada)
            if Modifier.BLOCKED in modifiers:
                columns["blocked"].append(issue_entry)
            else:
                columns[col].append(issue_entry)

    return columns


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
        State.TODO: "todo",
        State.DEV: "dev",
        State.REVIEW: "review",
        State.QA: "reviewed",  # QA é pós-review = coluna "reviewed" no kanban
        State.DONE: "done",
        State.SPEC: None,   # spec não aparece no kanban
        State.READY: None,  # ready não aparece no kanban
    }
    if not isinstance(state, State):
        return None
    return mapping.get(state)


def _empty_columns() -> dict[str, list[dict]]:
    """Retorna as colunas vazias do kanban."""
    return {
        "todo": [],
        "dev": [],
        "review": [],
        "reviewed": [],
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


async def handle_issues(request: web.Request, ctx: object = None) -> web.Response:
    """Lista issues por estágio (crewflow:*). Zero-token, cache-first."""
    try:
        loop = asyncio.get_running_loop()
        columns = await loop.run_in_executor(None, _load_issues_from_github)
        return web.json_response({"columns": columns})
    except Exception as exc:
        logger.exception("handle_issues: erro inesperado: %s", exc)
        return web.json_response(
            {"error": str(exc), "columns": _empty_columns()},
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
    """Força o dispatch de uma issue: marca crewflow:todo e dispara o estágio dev.

    Reutiliza BackendCronCtx e o mesmo caminho do _run_stage.
    """
    app_root = Path(__file__).parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    from flow.adapters import github_client as gh
    from flow.domain.state import Modifier, State, parse_modifiers, parse_state
    from flow.ports.issue_provider import ProviderError, ProviderNotFoundError

    # Busca o estado atual da issue
    try:
        item = gh.get_work_item(repo, str(issue_number))
    except ProviderNotFoundError:
        return {"ok": False, "error": f"issue #{issue_number} não encontrada em {repo!r}"}
    except ProviderError as exc:
        return {"ok": False, "error": f"erro ao acessar a issue: {exc}"}

    current_labels = set(item.get("labels", []))
    state = parse_state(current_labels)
    modifiers = parse_modifiers(current_labels)

    # Se já está em crewflow:todo e sem modificadores de parada, dispara direto
    # Se está em outro estado, marca crewflow:todo primeiro
    if state is not State.TODO or Modifier.BLOCKED in modifiers or Modifier.RUNNING in modifiers:
        # Preserva labels de tipo/prioridade, só troca o estado
        from flow.domain.state import transition_state
        new_labels = transition_state(
            current_labels,
            State.TODO,
        )
        # Remove modificadores de parada para garantir dispatchability
        new_labels = new_labels - {Modifier.BLOCKED.value, Modifier.RUNNING.value}
        try:
            gh.set_labels(repo, str(issue_number), sorted(new_labels))
            logger.info(
                "force_dispatch: %s#%s → crewflow:todo (era %s)",
                repo, issue_number, state,
            )
        except ProviderError as exc:
            return {"ok": False, "error": f"erro ao marcar crewflow:todo: {exc}"}

    # Dispara o estágio dev via deployment._run_stage
    try:
        from backend.ctx import BackendCronCtx
        from deployment.deployment import _STAGE_DEV, _run_stage

        ctx = BackendCronCtx()
        _run_stage(ctx, _STAGE_DEV)
        logger.info("force_dispatch: _run_stage(dev) executado para %s#%s", repo, issue_number)
    except Exception as exc:
        logger.warning(
            "force_dispatch: _run_stage falhou para %s#%s: %s — issue marcada mas não despachada",
            repo, issue_number, exc,
        )
        return {
            "ok": True,
            "dispatched": False,
            "note": f"issue marcada crewflow:todo, mas dispatch falhou: {exc}",
        }

    return {"ok": True, "dispatched": True}
