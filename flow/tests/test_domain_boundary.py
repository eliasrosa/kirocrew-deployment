"""Teste de fronteira arquitetural.

Garante que `flow/domain/` não importa infraestrutura. Esta é a propriedade
que torna todo o domínio testável sem mock nenhum — e é a violação que
acontece sozinha com o tempo (alguém precisa de um dado, importa o cliente,
e o núcleo deixa de ser isolado).

Melhor descobrir no CI do que em meia hora de debug tentando entender por que
um teste de domínio precisa de credenciais.
"""

import ast
from pathlib import Path

DOMAIN_DIR = Path(__file__).parent.parent / "domain"

# Imports que nunca devem aparecer em flow/domain/
FORBIDDEN_IMPORTS = [
    "flow.adapters",
    "aiohttp",
    "subprocess",
    "socket",
    "urllib.request",
    "http.client",
    "requests",
    "httpx",
    "boto3",
    "botocore",
]

# Módulos de stdlib que têm I/O mas são aceitáveis no domínio
# (ex: pathlib pra constantes de caminho, se necessário)
ALLOWED_STDLIB_IO: set[str] = set()  # nenhum por enquanto; adicionar com critério


def _get_imports(source: str) -> list[str]:
    """Extrai todos os nomes de módulo importados num arquivo Python."""
    tree = ast.parse(source)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
                found.append(node.module)
    return found


def test_domain_nao_importa_adapters() -> None:
    """flow/domain/ não pode importar flow.adapters."""
    for arquivo in DOMAIN_DIR.rglob("*.py"):
        imports = _get_imports(arquivo.read_text(encoding="utf-8"))
        for imp in imports:
            assert not imp.startswith("flow.adapters"), (
                f"\n\n"
                f"VIOLAÇÃO DE FRONTEIRA ARQUITETURAL\n"
                f"  Arquivo : {arquivo.relative_to(DOMAIN_DIR.parent.parent)}\n"
                f"  Import  : {imp}\n"
                f"  Problema: domain/ não pode importar adapters/.\n"
                f"  Motivo  : a separação é o que torna os testes de domínio\n"
                f"            executáveis sem rede, disco ou credenciais.\n"
                f"  Solução : mova a lógica para ports/ ou use injeção de\n"
                f"            dependência no caller."
            )


def test_domain_nao_importa_infraestrutura() -> None:
    """flow/domain/ não pode importar bibliotecas de I/O."""
    for arquivo in DOMAIN_DIR.rglob("*.py"):
        imports = _get_imports(arquivo.read_text(encoding="utf-8"))
        for imp in imports:
            for proibido in FORBIDDEN_IMPORTS:
                assert not (imp == proibido or imp.startswith(proibido + ".")), (
                    f"\n\n"
                    f"VIOLAÇÃO DE FRONTEIRA ARQUITETURAL\n"
                    f"  Arquivo : {arquivo.relative_to(DOMAIN_DIR.parent.parent)}\n"
                    f"  Import  : {imp!r}\n"
                    f"  Proibido: {proibido!r}\n"
                    f"  Motivo  : este import introduz I/O no núcleo de domínio.\n"
                    f"  Solução : encapsule o I/O num adapter ou transport e\n"
                    f"            injete o resultado no domínio."
                )


def test_estrutura_de_pacotes_existe() -> None:
    """Os quatro pacotes da arquitetura devem existir e ser importáveis."""
    import importlib

    pacotes = [
        "flow",
        "flow.domain",
        "flow.ports",
        "flow.adapters",
        "flow.tests",
    ]
    for pacote in pacotes:
        modulo = importlib.import_module(pacote)
        assert modulo is not None, f"pacote {pacote!r} não importável"


def test_domain_tem_apenas_python_puro() -> None:
    """Todos os arquivos em domain/ devem ser parseáveis como Python puro.

    Captura arquivos com erros de sintaxe antes de qualquer execução.
    """
    for arquivo in DOMAIN_DIR.rglob("*.py"):
        fonte = arquivo.read_text(encoding="utf-8")
        try:
            ast.parse(fonte)
        except SyntaxError as e:
            raise AssertionError(
                f"Erro de sintaxe em {arquivo.relative_to(DOMAIN_DIR.parent.parent)}: {e}"
            ) from e
