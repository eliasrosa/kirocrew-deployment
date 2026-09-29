"""KiroCrew Flow — cron de verificação de update (gated).

Substitui o antigo ``flow_auto_update.py`` (a "gambiarra" que fazia
``git pull --rebase origin main`` **incondicionalmente** a cada 5 minutos e
podia auto-quebrar produção). Ver issue #247 (itens 3 e 4) e ``docs/RELEASE.md``.

Este cron **não** puxa o ``main`` cegamente. Ele:

1. Descobre o repositório via ``installed.json``.
2. Lê a versão instalada (``app.json`` do repo).
3. Descobre a versão do canal ``stable`` — faz ``git fetch --tags`` (uma falha
   aqui é tratada como **NOOP** duro, para nunca decidir sobre tags obsoletas) e
   resolve a tag imutável ``vX.Y.Z`` que o canal aponta.
4. Aplica a **política auto vs manual** pura de
   ``flow.domain.update_policy.decide_update_action``:
   - **Patch** (``fix``): ``AUTO_UPDATE`` → **avança o working tree** para a tag
     alvo (``git checkout <vX.Y.Z>``) e só então reinstala pelo caminho oficial
     (``./scripts/install-cron.sh``, o mesmo do ``setup.onUpdate`` do ``app.json``).
     Confirma que a versão avançou de fato antes de reportar sucesso.
   - **Minor/Major** (``feat``/breaking): ``NOTIFY_ONLY`` → **apenas notifica**
     via ``Report(msg)`` e **aguarda ação manual**. Nunca aplica sozinho.
   - **Igual/mais novo**: ``NOOP`` → encerra silenciosamente (``Skip()``).

O ponto crítico corrigido (ver review v1 e issue #247): o ``install-cron.sh``
apenas *copia* o checkout atual — ele nunca faz fetch/checkout. Portanto, para o
patch auto-aplicar de verdade, este cron **move o working tree para a tag alvo
antes** de reinstalar, e reconfirma a versão. Minor/major continuam apenas
notificando (o princípio inviolável: produção nunca é auto-quebrada).

Instalação: copiado por ``scripts/install-cron.sh`` para
``~/.kiro/crew/crons/flow_update_check.py``. Registrado via ``app.json``
(cron ``flow-update-check``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger(__name__)

# O gateway/cron patcha o sys.path para o repo (ver install-cron.sh). Em
# execução local (testes), garantimos que a raiz do repo esteja importável.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT_GUESS = os.path.dirname(_HERE)
if _REPO_ROOT_GUESS not in sys.path:
    sys.path.insert(0, _REPO_ROOT_GUESS)

from flow.domain.update_policy import (  # noqa: E402
    UpdateAction,
    Version,
    decide_update_action,
    parse_version,
)

# Sentinel usado pelo cron runner para controle do job.
# Importado dinamicamente para não depender de kiro_crew no path de importação.
try:
    from kiro_crew.cron_script import Done, Report, Skip  # type: ignore[import]
except ImportError:
    # Fallback simples para ambientes sem kiro_crew no path (testes locais).
    class Skip(Exception):  # type: ignore[no-redef]
        """Sinaliza ao runner que nada precisa ser feito neste ciclo."""

    class Done(Exception):  # type: ignore[no-redef]
        """Sinaliza ao runner que o job concluiu e pode ser removido."""
        def __init__(self, msg: str = "") -> None:
            self.msg = msg

    class Report(Exception):  # type: ignore[no-redef]
        """Sinaliza ao runner que o job concluiu mas deve continuar agendado."""
        def __init__(self, msg: str = "") -> None:
            self.msg = msg


#: Canal comparado por padrão (ver docs/RELEASE.md → Tags de canal).
DEFAULT_CHANNEL = "stable"

#: Tags imutáveis de versão têm o formato ``vX.Y.Z`` (``tag_format = "v{version}"``
#: no python-semantic-release). Casamos estritamente para não pegar tags soltas
#: (``latest``, ``stable``, ``verify-x``, ...) que porventura apontem para o SHA.
_VERSION_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


def _find_repo_root() -> str | None:
    """Descobre o path do repositório via installed.json do app."""
    installed_json = os.path.expanduser(
        "~/.kiro/crew/apps/kirocrew-flow/installed.json"
    )
    if not os.path.exists(installed_json):
        logger.warning(
            "flow_update_check: installed.json não encontrado em %s", installed_json
        )
        return None
    try:
        with open(installed_json, encoding="utf-8") as f:
            data = json.load(f)
        source = data.get("source", "")
        if source and os.path.isdir(source):
            return source
        logger.warning(
            "flow_update_check: campo 'source' ausente ou inválido em installed.json: %r",
            source,
        )
    except Exception as exc:
        logger.warning("flow_update_check: erro ao ler installed.json: %s", exc)
    return None


def _installed_version(repo_root: str) -> str | None:
    """Lê a versão instalada a partir do app.json do repo."""
    app_json = os.path.join(repo_root, "app.json")
    try:
        with open(app_json, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("flow_update_check: erro ao ler app.json: %s", exc)
        return None
    value = data.get("version")
    return value if isinstance(value, str) and value else None


def _channel_version(repo_root: str, channel: str) -> str | None:
    """Descobre a versão do canal (tag movível ``stable``/``latest``) no remoto.

    Fluxo:

    1. ``git fetch --tags`` — se **falhar**, retorna ``None`` (tratado como NOOP
       duro pelo chamador). Nunca decidimos sobre tags locais obsoletas.
    2. ``git ls-remote --tags origin <channel>`` para obter o SHA da tag de canal.
    3. ``git tag --points-at <sha>`` e, dentre as tags que casam estritamente
       ``vX.Y.Z``, escolhe a **maior versão** (a ordem do ``git tag`` é arbitrária).

    Não altera o working tree (nenhum pull/checkout).
    """
    # 1. Atualiza as tags locais. Uma falha aqui é fatal para a decisão: seguir
    #    com tags obsoletas poderia comparar contra um canal desatualizado.
    try:
        fetch = subprocess.run(
            ["git", "fetch", "--tags", "--quiet", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("flow_update_check: git fetch --tags falhou: %s", exc)
        return None
    if fetch.returncode != 0:
        logger.warning(
            "flow_update_check: git fetch --tags falhou (rc=%d): %s — "
            "não decidiremos sobre tags obsoletas",
            fetch.returncode,
            fetch.stderr.strip(),
        )
        return None

    # 2. SHA que o canal aponta no remoto.
    try:
        ls = subprocess.run(
            ["git", "ls-remote", "--tags", "origin", channel],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("flow_update_check: git ls-remote falhou: %s", exc)
        return None

    if ls.returncode != 0 or not ls.stdout.strip():
        logger.warning(
            "flow_update_check: canal %r não encontrado no remoto (rc=%d)",
            channel,
            ls.returncode,
        )
        return None

    channel_sha = ls.stdout.split()[0]

    # 3. Tags imutáveis vX.Y.Z que apontam para o SHA do canal.
    try:
        points = subprocess.run(
            ["git", "tag", "--points-at", channel_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("flow_update_check: git tag --points-at falhou: %s", exc)
        return None

    best_tag: str | None = None
    best_ver: Version | None = None
    for line in points.stdout.splitlines():
        tag = line.strip()
        if not _VERSION_TAG_RE.match(tag):
            continue
        try:
            ver = parse_version(tag)
        except ValueError:
            continue
        if best_ver is None or ver > best_ver:
            best_ver = ver
            best_tag = tag
    return best_tag


def _checkout_target(repo_root: str, target_tag: str) -> None:
    """Avança o working tree para a tag imutável alvo (``vX.Y.Z``).

    Este é o passo que faltava: ``install-cron.sh`` apenas *copia* o checkout
    atual, então sem este ``git checkout`` o "auto-update" reinstalaria o mesmo
    código. Fazemos ``fetch`` da tag (defensivo — já foi buscada em
    ``_channel_version``, mas a chamamos de novo por robustez) e o checkout.

    Levanta :class:`Report` se qualquer passo do git falhar (o cron reporta e
    aguarda ação manual; nunca reporta sucesso sem o código ter avançado).
    """
    try:
        fetch = subprocess.run(
            ["git", "fetch", "--tags", "--quiet", "origin", target_tag],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise Report(
            f"⚠️ Update patch: git fetch da tag {target_tag} falhou ({exc}) — "
            "rode manualmente o hook oficial de update."
        )
    if fetch.returncode != 0:
        raise Report(
            f"⚠️ Update patch: git fetch da tag {target_tag} falhou "
            f"(rc={fetch.returncode}) — rode manualmente o hook oficial de update."
        )

    try:
        checkout = subprocess.run(
            ["git", "checkout", "--force", target_tag],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise Report(
            f"⚠️ Update patch: git checkout {target_tag} falhou ({exc}) — "
            "rode manualmente o hook oficial de update."
        )
    if checkout.returncode != 0:
        raise Report(
            f"⚠️ Update patch: git checkout {target_tag} falhou "
            f"(rc={checkout.returncode}): {checkout.stderr.strip()} — "
            "rode manualmente o hook oficial de update."
        )


def _run_install(repo_root: str) -> None:
    """Reinstala os scripts via ``./scripts/install-cron.sh`` (caminho oficial).

    Chamado **depois** de o working tree já ter avançado para a tag alvo, para
    que ``install-cron.sh`` copie o código novo (e não o antigo).
    """
    install_sh = os.path.join(repo_root, "scripts", "install-cron.sh")
    if not os.path.isfile(install_sh):
        raise Report(
            "⚠️ Update patch disponível mas install-cron.sh não encontrado em "
            f"{install_sh} — rode manualmente o hook oficial de update."
        )
    try:
        result = subprocess.run(
            ["bash", install_sh],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise Report(
            f"⚠️ Update patch: install-cron.sh falhou ({exc}) — rode manualmente."
        )
    if result.returncode != 0:
        raise Report(
            f"⚠️ Update patch: install-cron.sh falhou (rc={result.returncode}) — "
            "rode manualmente o hook oficial de update."
        )


def _run_official_update(repo_root: str, target: Version, target_tag: str) -> None:
    """Executa o caminho oficial de update para um release de **patch**.

    Ordem correta (o defeito do review v1 era pular o checkout):

    1. ``git checkout <vX.Y.Z>`` — avança o working tree para a versão alvo.
    2. ``./scripts/install-cron.sh`` — reinstala os scripts a partir do código já
       avançado (o mesmo do ``setup.onUpdate``).
    3. Reconfirma que a versão instalada avançou para ``target``; se não avançou,
       reporta em vez de anunciar um sucesso falso.
    """
    _checkout_target(repo_root, target_tag)
    _run_install(repo_root)

    # Confirma que o código realmente avançou (o app.json do checkout novo deve
    # bater com a target). Se não bateu, algo deu errado — reporta, não mente.
    confirmed_raw = _installed_version(repo_root)
    if confirmed_raw is None:
        raise Report(
            f"⚠️ Update patch para {target_tag} aplicado mas não foi possível "
            "reconfirmar a versão no app.json — verifique manualmente."
        )
    try:
        confirmed = parse_version(confirmed_raw)
    except ValueError:
        raise Report(
            f"⚠️ Update patch para {target_tag}: versão reconfirmada inválida "
            f"({confirmed_raw!r}) — verifique manualmente."
        )
    if confirmed < target:
        raise Report(
            f"⚠️ Update patch para {target_tag} não avançou o código "
            f"(ainda em {confirmed}) — verifique manualmente o hook oficial."
        )


def run(ctx: object) -> None:
    """Entrypoint do cron de verificação de update (gated pela política).

    - ``NOOP``: ``Skip()`` (silencioso).
    - ``NOTIFY_ONLY`` (minor/major): ``Report(msg)`` sem aplicar nada.
    - ``AUTO_UPDATE`` (patch): executa o update oficial e ``Report`` sucesso.
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        logger.error(
            "flow_update_check: não foi possível determinar o diretório do repo — "
            "verifique installed.json em ~/.kiro/crew/apps/kirocrew-flow/"
        )
        raise Skip()

    installed_raw = _installed_version(repo_root)
    if installed_raw is None:
        logger.warning("flow_update_check: versão instalada indisponível")
        raise Skip()

    target_raw = _channel_version(repo_root, DEFAULT_CHANNEL)
    if target_raw is None:
        # Sem canal stable publicado ainda — nada a decidir.
        raise Skip()

    try:
        installed = parse_version(installed_raw)
        target = parse_version(target_raw)
    except ValueError as exc:
        logger.warning("flow_update_check: versão inválida: %s", exc)
        raise Skip()

    decision = decide_update_action(installed, target)

    if decision.action is UpdateAction.NOOP:
        raise Skip()

    if decision.action is UpdateAction.NOTIFY_ONLY:
        # Minor/major: NUNCA auto-aplica. Só notifica e aguarda ação manual.
        # A atualização manual precisa AVANÇAR o código para a tag alvo antes de
        # reinstalar — apenas rodar install-cron.sh reinstalaria o checkout atual.
        raise Report(
            f"🔔 KiroCrew Flow: update {installed} → {target} disponível no canal "
            f"'{DEFAULT_CHANNEL}'. {decision.reason} "
            f"Atualização manual (após revisar as mudanças): rode o update oficial do "
            f"Crew App (o hook setup.onUpdate) ou, no clone, "
            f"`git checkout {target_raw} && ./scripts/install-cron.sh` — "
            f"o checkout da tag {target_raw} é obrigatório para o código de fato avançar."
        )

    # AUTO_UPDATE (patch): avança o working tree para a tag e reinstala.
    _run_official_update(repo_root, target, target_raw)
    raise Report(
        f"✅ KiroCrew Flow auto-atualizado (patch {installed} → {target}): "
        f"{decision.reason}"
    )
