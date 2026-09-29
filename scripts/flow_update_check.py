"""KiroCrew Flow — cron de verificação de update (gated).

Substitui o antigo ``flow_auto_update.py`` (a "gambiarra" que fazia
``git pull --rebase origin main`` **incondicionalmente** a cada 5 minutos e
podia auto-quebrar produção). Ver issue #247 (itens 3 e 4) e ``docs/RELEASE.md``.

Este cron **não** puxa o ``main`` cegamente. Ele:

1. Descobre o repositório via ``installed.json``.
2. Lê a versão instalada (``app.json`` do repo).
3. Descobre a versão do canal ``stable`` (tag ``stable`` no remoto).
4. Aplica a **política auto vs manual** pura de
   ``flow.domain.update_policy.decide_update_action``:
   - **Patch** (``fix``): ``AUTO_UPDATE`` → executa o caminho oficial de update
     (``./scripts/install-cron.sh``, o mesmo do ``setup.onUpdate`` do ``app.json``)
     e reporta sucesso.
   - **Minor/Major** (``feat``/breaking): ``NOTIFY_ONLY`` → **apenas notifica**
     via ``Report(msg)`` e **aguarda ação manual**. Nunca aplica sozinho.
   - **Igual/mais novo**: ``NOOP`` → encerra silenciosamente (``Skip()``).

O update de fato acontece pelo **hook oficial** ``setup.onUpdate`` do Crew App
(disparado pelo gateway) — este cron apenas decide, para releases de patch, se
o mecanismo oficial pode rodar automaticamente, e para minor/major apenas
notifica. Toda a decisão vem da lógica pura de domínio, sem I/O, testável sem
mock.

Instalação: copiado por ``scripts/install-cron.sh`` para
``~/.kiro/crew/crons/flow_update_check.py``. Registrado via ``app.json``
(cron ``flow-update-check``).
"""

from __future__ import annotations

import json
import logging
import os
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

    Usa ``git ls-remote --tags origin <channel>`` para obter o SHA da tag de
    canal e, em seguida, ``git tag --points-at`` para achar a tag imutável
    ``vX.Y.Z`` que ela referencia. Não altera o working tree (nenhum pull/checkout).
    """
    try:
        ls = subprocess.run(
            ["git", "ls-remote", "--tags", "origin", channel],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
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

    # Busca (sem checkout) as tags que apontam para o SHA do canal.
    try:
        subprocess.run(
            ["git", "fetch", "--tags", "--quiet", "origin"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        points = subprocess.run(
            ["git", "tag", "--points-at", channel_sha],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("flow_update_check: git tag --points-at falhou: %s", exc)
        return None

    for line in points.stdout.splitlines():
        tag = line.strip()
        if tag and tag not in ("stable", "latest") and (tag[0] in "vV" or tag[0].isdigit()):
            return tag
    return None


def _run_official_update(repo_root: str) -> None:
    """Executa o caminho oficial de update (o mesmo do setup.onUpdate).

    Reinstala os scripts via ``./scripts/install-cron.sh`` — só é chamado para
    releases de patch, quando a política autoriza o auto-update.
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
        raise Report(
            f"🔔 KiroCrew Flow: update {installed} → {target} disponível no canal "
            f"'{DEFAULT_CHANNEL}'. {decision.reason} "
            f"Atualização manual: rode o hook oficial de update (./scripts/install-cron.sh) "
            f"após revisar as mudanças."
        )

    # AUTO_UPDATE (patch): aplica pelo caminho oficial.
    _run_official_update(repo_root)
    raise Report(
        f"✅ KiroCrew Flow auto-atualizado (patch {installed} → {target}): "
        f"{decision.reason}"
    )
