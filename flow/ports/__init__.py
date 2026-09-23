"""Porta do KiroCrew Flow — o contrato que Jira e GitHub satisfazem.

O Protocol e os tipos de retorno normalizados vivem aqui.
O dispatch (dict provider → módulo) fica em `_dispatch.py`.

Os adapters são MÓDULOS que satisfazem o Protocol, não classes.
Isso significa que a conformidade não pode ser verificada estaticamente —
ver `tests/test_provider_parity.py` para o gate que faz isso em runtime.
"""
