"""Testa que o app.json declara os recursos no caminho esperado pelo gateway.

O gateway (hooks_integration.py) lê rotas de apps de terceiros em
``backend.hooks.routes``, não em ``backend.routes`` (que é ignorado para
apps não-builtins). Este teste previne regressão onde a key era declarada
no caminho errado, fazendo com que as rotas nunca fossem registradas ao
habilitar o app.

Ref: issue #170 — backend.routes nao wira rotas para apps de terceiros.
"""
from __future__ import annotations

import json
from pathlib import Path

# Caminho para o app.json na raiz do repo
_REPO_ROOT = Path(__file__).parent.parent.parent
_APP_JSON = _REPO_ROOT / "app.json"


def _load_manifest() -> dict:
    return json.loads(_APP_JSON.read_text(encoding="utf-8"))


class TestAppJsonManifest:
    """Valida que o app.json tem a estrutura correta para registro no gateway."""

    def test_app_json_exists(self) -> None:
        assert _APP_JSON.is_file(), f"app.json não encontrado em {_APP_JSON}"

    def test_backend_hooks_routes_declared(self) -> None:
        """backend.hooks.routes deve ser declarado — é o caminho que o gateway lê.

        O gateway (kiro_crew/apps/hooks_integration.py) invoca as rotas do app via:
            hooks = manifest.get("backend", {}).get("hooks", {})
            routes_hook = hooks.get("routes", "")

        Declarar em ``backend.routes`` (sem o nível ``hooks``) faz com que
        ``hooks_integration.on_app_enable`` e ``on_gateway_startup`` ignorem a
        declaração silenciosamente, e as rotas nunca são registradas.
        """
        manifest = _load_manifest()
        hooks = manifest.get("backend", {}).get("hooks", {})
        routes = hooks.get("routes", "")
        assert routes, (
            "app.json deve declarar 'backend.hooks.routes' com o hook de registro de rotas. "
            "Declarar em 'backend.routes' não funciona para apps de terceiros — o gateway "
            "lê apenas 'backend.hooks.routes'."
        )

    def test_backend_hooks_lifecycle_declared(self) -> None:
        """on_startup e on_shutdown devem ser declarados sob backend.hooks.

        O gateway (kiro_crew/apps/lifecycle.py) invoca esses hooks como
        ``func(ctx)`` quando o app é habilitado/desabilitado. Sem eles
        declarados, os 4 loops de polling da esteira nunca sobem sob o gateway.
        Ref: issue #170.
        """
        manifest = _load_manifest()
        hooks = manifest.get("backend", {}).get("hooks", {})
        assert hooks.get("on_startup"), (
            "app.json deve declarar 'backend.hooks.on_startup' apontando para o "
            "callable de startup (ex.: 'backend.hooks:on_startup')."
        )
        assert hooks.get("on_shutdown"), (
            "app.json deve declarar 'backend.hooks.on_shutdown' apontando para o "
            "callable de shutdown (ex.: 'backend.hooks:on_shutdown')."
        )
        for key in ("on_startup", "on_shutdown"):
            assert ":" in hooks[key], (
                f"backend.hooks.{key} deve estar no formato 'module.path:callable_name', "
                f"obtido: {hooks[key]!r}"
            )

    def test_backend_hooks_routes_format(self) -> None:
        """backend.hooks.routes deve estar no formato 'module.path:callable_name'."""
        manifest = _load_manifest()
        routes = manifest.get("backend", {}).get("hooks", {}).get("routes", "")
        assert ":" in routes, (
            f"backend.hooks.routes deve estar no formato 'module.path:callable_name', "
            f"obtido: {routes!r}"
        )
        module_path, callable_name = routes.rsplit(":", 1)
        assert module_path, "Caminho do módulo não pode ser vazio"
        assert callable_name, "Nome do callable não pode ser vazio"

    def test_backend_routes_not_declared_at_top_level(self) -> None:
        """backend.routes (sem hooks) deve estar ausente — é o caminho que NÃO funciona.

        Verificar que não estamos declarando no caminho errado acidentalmente.
        O caminho incorreto seria: {"backend": {"routes": "..."}} sem o nível "hooks".
        """
        manifest = _load_manifest()
        backend = manifest.get("backend", {})
        # Aceitar que backend.routes pode existir como base path (string vazia ou URL)
        # mas NÃO deve conter o hook path no formato "module:callable"
        top_level_routes = backend.get("routes", "")
        if top_level_routes and ":" in top_level_routes:
            raise AssertionError(
                f"app.json tem 'backend.routes' com valor de hook ({top_level_routes!r}). "
                "Este campo é ignorado para apps de terceiros — use 'backend.hooks.routes'."
            )

    def test_routes_module_file_exists(self) -> None:
        """O arquivo do módulo declarado em backend.hooks.routes deve existir."""
        manifest = _load_manifest()
        routes_hook = manifest.get("backend", {}).get("hooks", {}).get("routes", "")
        if not routes_hook or ":" not in routes_hook:
            return  # validado por outro teste
        module_path = routes_hook.rsplit(":", 1)[0]
        rel_path = module_path.replace(".", "/") + ".py"
        full_path = _REPO_ROOT / rel_path
        assert full_path.is_file(), (
            f"Módulo declarado em backend.hooks.routes não encontrado: {full_path}. "
            f"Hook: {routes_hook!r}"
        )

    def test_routes_callable_exists_in_module(self) -> None:
        """O callable declarado em backend.hooks.routes deve existir no módulo."""
        import importlib.util
        import sys

        manifest = _load_manifest()
        routes_hook = manifest.get("backend", {}).get("hooks", {}).get("routes", "")
        if not routes_hook or ":" not in routes_hook:
            return

        module_path, callable_name = routes_hook.rsplit(":", 1)
        rel_path = module_path.replace(".", "/") + ".py"
        full_path = _REPO_ROOT / rel_path

        if not full_path.is_file():
            return  # validado por outro teste

        # Adicionar repo root ao sys.path para resolver imports relativos
        repo_str = str(_REPO_ROOT)
        added = repo_str not in sys.path
        if added:
            sys.path.insert(0, repo_str)
        try:
            spec = importlib.util.spec_from_file_location(
                f"_test_app_manifest_{module_path}", full_path
            )
            assert spec is not None
            mod = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            assert hasattr(mod, callable_name), (
                f"Callable {callable_name!r} não encontrado no módulo {module_path} "
                f"({full_path})"
            )
            assert callable(getattr(mod, callable_name)), (
                f"{callable_name!r} em {module_path} não é um callable"
            )
        finally:
            if added and repo_str in sys.path:
                sys.path.remove(repo_str)
