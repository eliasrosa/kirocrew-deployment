"""flow.prompts — loader de templates MD para sessões one-shot.

Cada estágio (dev, reviewer, …) tem seu próprio arquivo MD editável em
``flow/prompts/<stage>.md``. Este módulo carrega e interpola esses templates,
validando que todas as variáveis referenciadas estejam presentes antes de
retornar o prompt — fail-closed: variável faltando levanta ``PromptRenderError``
em vez de mandar prompt incompleto.

Uso::

    from flow.prompts.loader import render_prompt

    prompt = render_prompt(
        "dev",
        repo="owner/repo",
        issue_number=42,
        issue_title="Fix bug",
        ...
    )

Templates em disco podem ser editados livremente. Se o template de um estágio
não existir no disco (apagado ou corrompido), o loader usa o **fallback embutido**
— o conteúdo original versionado no módulo.

Variáveis usam a sintaxe ``{{nome_da_variavel}}``.
"""

from __future__ import annotations

import re
from pathlib import Path

# Diretório onde ficam os templates MD
_PROMPTS_DIR = Path(__file__).parent

# Regex para detectar placeholders {{variavel}}
_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_][a-zA-Z0-9_]*)\}\}")


class PromptRenderError(Exception):
    """Levantada quando o template referencia uma variável não fornecida.

    O dispatch é fail-closed: não envia prompt quebrado.
    """


def _load_template(stage: str) -> str:
    """Carrega o template MD do disco para o estágio dado.

    Se o arquivo não existir ou não puder ser lido, levanta ``FileNotFoundError``
    para que o chamador aplique o fallback embutido.
    """
    path = _PROMPTS_DIR / f"{stage}.md"
    return path.read_text(encoding="utf-8")


def _validate_and_render(template: str, variables: dict[str, str]) -> str:
    """Interpola ``variables`` no ``template`` e valida que nenhum placeholder sobrou.

    Levanta ``PromptRenderError`` se o template referenciar uma variável ausente.
    """
    # Interpola todas as variáveis conhecidas
    result = template
    for key, value in variables.items():
        result = result.replace(f"{{{{{key}}}}}", value)

    # Verifica placeholders que sobraram (variável não fornecida)
    remaining = _PLACEHOLDER_RE.findall(result)
    if remaining:
        missing = sorted(set(remaining))
        raise PromptRenderError(
            f"Template '{template[:40]}...' referencia variável(is) não fornecida(s): "
            + ", ".join(missing)
        )

    return result


def render_prompt(stage: str, fallback: str | None = None, **variables: str) -> str:
    """Carrega e renderiza o template do estágio ``stage`` com as ``variables`` dadas.

    Args:
        stage: nome do estágio (ex: ``"dev"``, ``"reviewer"``). Corresponde ao
            arquivo ``flow/prompts/<stage>.md``.
        fallback: conteúdo de fallback usado quando o template não existe no disco
            ou não pode ser lido. Se ``None`` e o template estiver ausente, levanta
            ``FileNotFoundError``.
        **variables: variáveis de interpolação. Qualquer ``{{nome}}`` no template
            deve ter um correspondente aqui; caso contrário ``PromptRenderError``
            é levantada.

    Returns:
        O prompt renderizado e validado.

    Raises:
        FileNotFoundError: se o template não existir e nenhum fallback for fornecido.
        PromptRenderError: se o template referenciar variável não fornecida.
    """
    try:
        template = _load_template(stage)
    except (FileNotFoundError, OSError) as exc:
        if fallback is None:
            raise FileNotFoundError(
                f"Template de prompt '{stage}' não encontrado em {_PROMPTS_DIR / stage}.md "
                "e nenhum fallback foi fornecido. Dispatch abortado."
            ) from exc
        template = fallback

    return _validate_and_render(template, variables)
