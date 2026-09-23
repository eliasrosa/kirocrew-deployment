"""Adapters do KiroCrew Flow.

Cada adapter é um MÓDULO com três camadas:
  *_client.py       — orquestração; satisfaz o Protocol de ports/
  *_transport.py    — I/O bruto; fachada patchável nos testes
  *_normalization.py — payload do provedor → contrato canônico

Separar o transport é o que torna os testes unitários viáveis sem rede:
os testes usam mock.patch.object no transport, não interceptam HTTP.
"""
