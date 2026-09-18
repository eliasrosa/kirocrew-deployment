"""Adapter Jira para KiroCrew Flow.

Satisfaz o Protocol ``IssueProvider`` de ``flow.ports.issue_provider``.
Este módulo é composto por três camadas:

  jira_transport.py    — cliente REST via MCP ou API (fachada patchável nos testes)
  jira_normalization.py — payload do Jira → contrato canônico
  jira_client.py       — orquestração; implementa a superfície da porta

Peculiaridades do VGAT que a normalização deve absorver (ver #19):
  • Task NÃO funciona no VGAT — usar Enabler
  • "Débito Técnico" tem acento obrigatório no tipo
  • description em ADF não pode ser passada na criação
  • subtask precisa de parent como string direta

TODO: implementação em andamento (#19).
"""

from __future__ import annotations

from flow.ports.issue_provider import ProviderNotFoundError, ProviderSetupError  # noqa: F401


def get_work_item(project: str, key: str) -> dict:
    raise NotImplementedError("Jira adapter — get_work_item (#19)")


def list_by_state(project: str, state: str) -> list[dict]:
    raise NotImplementedError("Jira adapter — list_by_state (#19)")


def list_changed_since(project: str, since_hash: str) -> list[dict]:
    raise NotImplementedError("Jira adapter — list_changed_since (#19)")


def set_labels(project: str, key: str, labels: list[str]) -> None:
    raise NotImplementedError("Jira adapter — set_labels (#19)")


def upsert_state_comment(project: str, key: str, body: str) -> None:
    raise NotImplementedError("Jira adapter — upsert_state_comment (#19)")


def get_state_comment(project: str, key: str) -> str | None:
    raise NotImplementedError("Jira adapter — get_state_comment (#19)")
