"""Política de update auto vs manual do KiroCrew Flow.

Este módulo é o núcleo do item 4 da issue #247 (ver ``docs/RELEASE.md`` →
*Decisão: auto vs manual*). Ele codifica, como **lógica pura sem I/O**, a
decisão de quando um update pode se aplicar sozinho e quando precisa apenas
notificar e aguardar uma ação humana.

A política deriva diretamente do **princípio inviolável** do projeto
(``.kiro/steering/fluxo.md`` e a seção *Princípios* do ``README.md``): a
automação nunca faz deploy e a produção jamais pode ser quebrada
automaticamente. A consequência prática:

- **Patch** (``fix:`` — compatível para trás): ``AUTO_UPDATE`` — pode aplicar
  automaticamente.
- **Minor** (``feat:`` — pode mudar comportamento): ``NOTIFY_ONLY`` — apenas
  notifica e aguarda ação manual.
- **Major / breaking** (``feat!:`` / ``BREAKING CHANGE:``): ``NOTIFY_ONLY`` —
  apenas notifica e aguarda ação manual.
- Versão instalada **igual** à target, ou **mais nova** que a target: ``NOOP``
  — nada a fazer.

Todo este módulo é puro Python sem I/O — testável sem mock nenhum. Ele vive em
``flow/domain/`` e, por contrato (``test_domain_boundary.py``), não importa
nenhuma biblioteca de I/O: a comparação de versões é feita apenas com parsing
de string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "UpdateAction",
    "UpdateDecision",
    "Version",
    "decide_update_action",
    "parse_version",
]


# ---------------------------------------------------------------------------
# Ação resultante da política
# ---------------------------------------------------------------------------

class UpdateAction(StrEnum):
    """O que o mecanismo de update deve fazer diante de uma versão target.

    - ``AUTO_UPDATE``: update compatível para trás (patch/``fix``) — pode
      aplicar automaticamente pelo hook oficial.
    - ``NOTIFY_ONLY``: update que pode mudar comportamento (minor/major) —
      apenas notifica o usuário e aguarda ação manual; nunca aplica sozinho.
    - ``NOOP``: já está atualizado (ou a versão instalada é mais nova que a
      target) — nada a fazer.
    """

    AUTO_UPDATE = "auto_update"
    NOTIFY_ONLY = "notify_only"
    NOOP        = "noop"


# ---------------------------------------------------------------------------
# Versão semântica (parsing puro, sem dependências)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True, order=True)
class Version:
    """Versão semântica ``MAJOR.MINOR.PATCH``.

    Comparável por ``order=True`` (compara a tupla ``(major, minor, patch)``).
    Pré-release e metadados de build são ignorados na comparação de nível —
    a política de canal ``stable`` compara apenas o trio numérico, coerente
    com ``tag_format = "v{version}"`` do ``python-semantic-release``.
    """

    major: int = 0
    minor: int = 0
    patch: int = 0

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_version(raw: str) -> Version:
    """Faz parsing de uma string de versão em um :class:`Version`.

    Aceita um prefixo ``v`` opcional (ex.: ``"v1.2.3"``), pré-release/build
    (``"1.2.3-rc.1+build5"``) e versões parciais (``"1"``, ``"1.2"``). Os
    componentes ausentes assumem ``0``.

    Levanta ``ValueError`` se a string não contiver ao menos um número de
    versão parseável.
    """
    if raw is None:
        raise ValueError("versão não pode ser None")

    text = raw.strip()
    if not text:
        raise ValueError("versão vazia")

    if text[0] in ("v", "V"):
        text = text[1:]

    # Descarta pré-release ("-rc.1") e metadados de build ("+abc")
    for sep in ("-", "+"):
        idx = text.find(sep)
        if idx != -1:
            text = text[:idx]

    parts = text.split(".")
    numbers: list[int] = []
    for raw_part in parts[:3]:
        part = raw_part.strip()
        if not part:
            break
        if not part.isdigit():
            raise ValueError(f"componente de versão inválido: {part!r} em {raw!r}")
        numbers.append(int(part))

    if not numbers:
        raise ValueError(f"versão sem componente numérico: {raw!r}")

    while len(numbers) < 3:
        numbers.append(0)

    return Version(numbers[0], numbers[1], numbers[2])


# ---------------------------------------------------------------------------
# Decisão da política
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UpdateDecision:
    """Resultado da política de update.

    ``action`` é o que fazer; ``reason`` explica em linguagem humana (vai para
    a notificação/auditoria); ``installed`` e ``target`` carregam as versões
    comparadas.
    """

    action: UpdateAction
    reason: str = ""
    installed: Version = field(default_factory=Version)
    target: Version = field(default_factory=Version)

    @property
    def is_auto(self) -> bool:
        return self.action is UpdateAction.AUTO_UPDATE

    @property
    def is_notify(self) -> bool:
        return self.action is UpdateAction.NOTIFY_ONLY


def decide_update_action(installed: Version, target: Version) -> UpdateDecision:
    """Decide a ação de update comparando a versão instalada com a target.

    A ``target`` é, por padrão, a versão do canal ``stable`` (ver
    ``docs/RELEASE.md`` → *Tags de canal*).

    Regras (derivadas do princípio inviolável — só patch auto-aplica):

    - ``target <= installed``            → ``NOOP`` (já atual ou mais novo).
    - ``target.major > installed.major`` → ``NOTIFY_ONLY`` (major/breaking).
    - ``target.minor > installed.minor`` (mesmo major) → ``NOTIFY_ONLY`` (minor/feat).
    - só ``target.patch > installed.patch`` (mesmo major e minor) → ``AUTO_UPDATE``.
    """
    if target <= installed:
        return UpdateDecision(
            action=UpdateAction.NOOP,
            reason=(
                f"versão instalada ({installed}) já está atual em relação à "
                f"target ({target}) — nada a fazer."
            ),
            installed=installed,
            target=target,
        )

    # A partir daqui, target > installed.
    if target.major > installed.major:
        return UpdateDecision(
            action=UpdateAction.NOTIFY_ONLY,
            reason=(
                f"update major disponível ({installed} → {target}): pode quebrar "
                f"compatibilidade. Notifica e aguarda ação manual — deploy é sempre "
                f"manual."
            ),
            installed=installed,
            target=target,
        )

    if target.minor > installed.minor:
        return UpdateDecision(
            action=UpdateAction.NOTIFY_ONLY,
            reason=(
                f"update minor disponível ({installed} → {target}): nova "
                f"funcionalidade pode mudar comportamento. Notifica e aguarda ação "
                f"manual — deploy é sempre manual."
            ),
            installed=installed,
            target=target,
        )

    # Mesmo major e minor, target maior → só pode ser incremento de patch.
    return UpdateDecision(
        action=UpdateAction.AUTO_UPDATE,
        reason=(
            f"update patch disponível ({installed} → {target}): correção "
            f"compatível para trás. Seguro para auto-aplicar."
        ),
        installed=installed,
        target=target,
    )
