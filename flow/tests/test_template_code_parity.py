"""Testes de paridade template↔código — garante que todo placeholder {{variavel}}
declarado em ``flow/prompts/<estágio>.md`` tem o argumento correspondente passado
pela função de dispatch em ``deployment/deployment.py``.

Se um template referenciar uma variável que o código não fornece, o CI quebra aqui
— antes de qualquer deploy ou reinstalação do cron.

Motivação: PR #112 adicionou ``{{head_sha}}`` ao reviewer.md mas o cron instalado
não foi reinstalado, causando PromptRenderError em produção (issue #116). Este teste
garante que futuras adições de placeholder sejam detectadas no CI.

Cobertura:
- dev.md    ↔ _dispatch_prompt()
- reviewer.md ↔ _reviewer_prompt()
- rework.md   ↔ _rework_prompt()
- conflict.md ↔ _conflict_prompt()
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

import pytest

# Caminhos canônicos
_REPO_ROOT = Path(__file__).parent.parent.parent
_PROMPTS_DIR = _REPO_ROOT / "flow" / "prompts"
_DEPLOYMENT_PY = _REPO_ROOT / "deployment" / "deployment.py"

# Regex para placeholders {{variavel}} nos templates
_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


# ---------------------------------------------------------------------------
# Helpers de extração
# ---------------------------------------------------------------------------


def _placeholders_in_template(stage: str) -> frozenset[str]:
    """Extrai o conjunto de placeholders {{...}} do template MD de um estágio."""
    path = _PROMPTS_DIR / f"{stage}.md"
    if not path.exists():
        return frozenset()
    text = path.read_text(encoding="utf-8")
    return frozenset(_PLACEHOLDER_RE.findall(text))


def _kwargs_in_render_call(stage: str) -> frozenset[str]:
    """Extrai os kwargs passados a render_prompt(stage, ...) no deployment.py via AST.

    Cada chamada render_prompt("dev", ...) ou render_prompt("reviewer", ...) é
    inspecionada; os keyword arguments (exceto ``stage`` posicional e ``fallback``)
    são o conjunto de variáveis que o código fornece ao template.

    Retorna frozenset de nomes de kwarg, ou frozenset() se nenhuma chamada for
    encontrada para o estágio dado.
    """
    source = _DEPLOYMENT_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)

    collected: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Detecta render_prompt(...)
        func = node.func
        func_name = ""
        if isinstance(func, ast.Name):
            func_name = func.id
        elif isinstance(func, ast.Attribute):
            func_name = func.attr

        if func_name != "render_prompt":
            continue

        # Primeiro argumento posicional deve ser a string do estágio
        if not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or first.value != stage:
            continue

        # Coleta todos os kwargs (excluindo "fallback" — é controle interno do loader)
        for kw in node.keywords:
            if kw.arg is not None and kw.arg != "fallback":
                collected.add(kw.arg)

    return frozenset(collected)


# ---------------------------------------------------------------------------
# Casos de teste por estágio
# ---------------------------------------------------------------------------


class _ParityCase(NamedTuple):
    stage: str
    dispatch_fn: str  # nome da função de dispatch, para mensagem de erro


_STAGES: list[_ParityCase] = [
    _ParityCase("dev", "_dispatch_prompt"),
    _ParityCase("reviewer", "_reviewer_prompt"),
    _ParityCase("rework", "_rework_prompt"),
    _ParityCase("conflict", "_conflict_prompt"),
]


@pytest.mark.parametrize("case", _STAGES, ids=[s.stage for s in _STAGES])
def test_todos_placeholders_do_template_sao_fornecidos_pelo_codigo(
    case: _ParityCase,
) -> None:
    """Cada {{variavel}} no template.md tem um kwarg correspondente no dispatch.

    Falha se um placeholder novo for adicionado ao MD sem atualizar a função de
    dispatch em deployment.py — o exato cenário que causou a issue #116.
    """
    template_path = _PROMPTS_DIR / f"{case.stage}.md"
    if not template_path.exists():
        pytest.skip(f"Template {case.stage}.md não existe — estágio ainda não implementado")

    placeholders = _placeholders_in_template(case.stage)
    provided = _kwargs_in_render_call(case.stage)

    missing_in_code = placeholders - provided

    assert not missing_in_code, (
        f"\n\n[PARIDADE TEMPLATE↔CÓDIGO — estágio '{case.stage}']\n"
        f"  Template: flow/prompts/{case.stage}.md\n"
        f"  Dispatch: deployment/deployment.py::{case.dispatch_fn}()\n\n"
        f"  Placeholders no template SEM variável no dispatch:\n"
        + "".join(f"    - {{{{ {v} }}}}\n" for v in sorted(missing_in_code))
        + f"\n  Ação: adicione '{', '.join(sorted(missing_in_code))}' como argumento em "
        f"{case.dispatch_fn}() ou remova o placeholder do template.\n"
        f"  Após corrigir, reinstale o cron: ./scripts/install-cron.sh\n"
    )


@pytest.mark.parametrize("case", _STAGES, ids=[s.stage for s in _STAGES])
def test_deployment_py_pode_ser_parseado_como_ast(case: _ParityCase) -> None:
    """Garante que deployment.py é válido Python para o parser AST usado nos testes de paridade."""
    source = _DEPLOYMENT_PY.read_text(encoding="utf-8")
    # parse() levanta SyntaxError se o arquivo estiver corrompido
    tree = ast.parse(source)
    assert tree is not None


@pytest.mark.parametrize("case", _STAGES, ids=[s.stage for s in _STAGES])
def test_dispatch_fornece_pelo_menos_as_variaveis_do_fallback(
    case: _ParityCase,
) -> None:
    """O conjunto de kwargs do dispatch deve cobrir as variáveis do fallback embutido.

    O fallback (string Python em deployment.py) também usa {{...}} e deve bater
    com os kwargs — senão o fallback já estaria quebrado sem o template em disco.
    """
    # Extrai o fallback pela convenção de nome _DEV_PROMPT_FALLBACK, _REWORK_PROMPT_FALLBACK etc.
    source = _DEPLOYMENT_PY.read_text(encoding="utf-8")

    # Tenta localizar o fallback pelo padrão de nome de constante
    fallback_patterns = {
        "dev": "_DEV_PROMPT_FALLBACK",
        "reviewer": "_reviewer_fallback",  # variável local, não constante de módulo
        "rework": "_REWORK_PROMPT_FALLBACK",
        "conflict": "_CONFLICT_PROMPT_FALLBACK",
    }
    const_name = fallback_patterns.get(case.stage)
    if const_name is None or const_name.startswith("_reviewer"):
        # reviewer usa variável local — coberto pelo teste de template
        pytest.skip(f"Fallback de '{case.stage}' é variável local — coberto pelo teste de template")

    # Extrai o valor da constante de nível de módulo via AST
    tree = ast.parse(source)
    fallback_text: str | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == const_name
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                fallback_text = node.value.value
            elif isinstance(node.value, (ast.JoinedStr, ast.BinOp)):
                # f-string ou concatenação — avalia o literal via ast.literal_eval
                import contextlib
                with contextlib.suppress(ValueError, TypeError):
                    _val = ast.literal_eval(node.value)
                    if isinstance(_val, str):
                        fallback_text = _val
            break

    if fallback_text is None:
        pytest.skip(f"Fallback '{const_name}' não encontrado como constante literal — pulando")

    fallback_placeholders = frozenset(_PLACEHOLDER_RE.findall(fallback_text))
    provided = _kwargs_in_render_call(case.stage)
    missing_in_fallback_code = fallback_placeholders - provided

    assert not missing_in_fallback_code, (
        f"\n\n[PARIDADE FALLBACK↔CÓDIGO — estágio '{case.stage}']\n"
        f"  Fallback: deployment/deployment.py::{const_name}\n"
        f"  Dispatch: deployment/deployment.py::{case.dispatch_fn}()\n\n"
        f"  Placeholders no fallback SEM variável no dispatch:\n"
        + "".join(f"    - {{{{ {v} }}}}\n" for v in sorted(missing_in_fallback_code))
    )
