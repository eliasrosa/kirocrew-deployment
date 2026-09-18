"""Adapter GitHub para KiroCrew Flow.

Satisfaz o Protocol ``IssueProvider`` de ``flow.ports.issue_provider``.
Este módulo é composto por três camadas:

  github_transport.py    — I/O bruto via ``gh`` CLI (fachada patchável nos testes)
  github_normalization.py — payload do GitHub → contrato canônico
  github_client.py       — orquestração; implementa a superfície da porta

TODO: implementação em andamento (#18).
"""

from __future__ import annotations

from flow.ports.issue_provider import ProviderNotFoundError, ProviderSetupError  # noqa: F401


def get_work_item(project: str, key: str) -> dict:
    raise NotImplementedError("GitHub adapter — get_work_item (#18)")


def list_by_state(project: str, state: str) -> list[dict]:
    raise NotImplementedError("GitHub adapter — list_by_state (#18)")


def list_changed_since(project: str, since_hash: str) -> list[dict]:
    raise NotImplementedError("GitHub adapter — list_changed_since (#18)")


def set_labels(project: str, key: str, labels: list[str]) -> None:
    raise NotImplementedError("GitHub adapter — set_labels (#18)")


def upsert_state_comment(project: str, key: str, body: str) -> None:
    raise NotImplementedError("GitHub adapter — upsert_state_comment (#18)")


def get_state_comment(project: str, key: str) -> str | None:
    raise NotImplementedError("GitHub adapter — get_state_comment (#18)")
