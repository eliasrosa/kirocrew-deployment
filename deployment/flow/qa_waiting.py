"""KiroCrew Flow — cron de notificação de QA.

Notifica QA quando uma issue entra em ``flow:qa-waiting``.

Pendente de implementação (issue #195): este módulo requer um novo estágio
``_STAGE_QA_NOTIFY`` em ``deployment.deployment`` com suporte a ``NOTIFY_HUMAN QA``
isolado. Por ora, o comportamento equivalente já está incluído no cron legado
``run(ctx)`` via ``_notify_human_actions`` (role ``HumanRole.QA``).

Registro (a habilitar após #195):
    cron_add(name="flow-qa-notify",
             script="~/.kiro/crew/crons/deployment/flow/qa_notify.py:run",
             every=300)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(ctx: object) -> None:
    """Entrypoint do cron de notificação de QA.

    Notifica QA para issues em ``flow:qa-waiting``.

    Este estágio ainda não está implementado de forma isolada.
    A notificação de QA ocorre via o cron legado ``run(ctx)`` em
    ``deployment.deployment`` (função ``_notify_human_actions``, role ``HumanRole.QA``).

    Para habilitar este cron de forma independente, implemente o suporte a
    ``_STAGE_QA_NOTIFY`` em ``deployment.deployment`` (issue #195).
    """
    logger.warning(
        "flow/qa_notify: estágio isolado de QA não implementado ainda — "
        "use o cron legado deployment.py:run ou aguarde a issue #195."
    )
