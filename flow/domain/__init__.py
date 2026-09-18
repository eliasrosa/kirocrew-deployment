"""Núcleo de domínio do KiroCrew Flow.

REGRA DE ISOLAMENTO (aplicada por test_domain_boundary.py):
Este pacote NÃO pode importar nada de `flow.adapters`, `flow.ports` (exceto
os tipos em `ports.__init__`), `aiohttp`, `subprocess` ou qualquer biblioteca
de I/O.

Consequência: todo código aqui é testável sem mock nenhum.
"""
