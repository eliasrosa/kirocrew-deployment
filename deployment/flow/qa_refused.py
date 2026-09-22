"""KiroCrew Flow — cron de QA reprovado (gate humano).

Notifica TL e dev quando uma issue entra em ``flow:qa-refused``.

Pendente de implementação (issue #195): este módulo requer um novo estágio
``_STAGE_QA_REFUSED`` em ``deployment.deployment`` com suporte a
``NOTIFY_HUMAN TL+DEV`` isolado para o estado qa-refused. Por ora, o
comportamento equivalente já está no cron legado ``run(ctx)`` via
``_notify_human_actions`` (role ``HumanRole.TL`` + ``HumanRole.DEV``).

Registro (a habilitar após #195):
    cron_add(name="flow-qa-refused",
             script="~/.kiro/crew/crons/deployment/flow/qa_refused.py:run",
             every=300)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(ctx: object) -> None:
    """Entrypoint do cron de QA reprovado.

    Notifica TL e dev para issues em ``flow:qa-refused`` (gate humano).

    Este estágio ainda não está implementado de forma isolada.
    A notificação ocorre via o cron legado ``run(ctx)`` em
    ``deployment.deployment`` (função ``_notify_human_actions``).

    Para habilitar este cron de forma independente, implemente o suporte a
    ``_STAGE_QA_REFUSED`` em ``deployment.deployment`` (issue #195).
    """
    logger.warning(
        "flow/qa_refused: estágio isolado de QA reprovado não implementado ainda — "
        "use o cron legado deployment.py:run ou aguarde a issue #195."
    )
