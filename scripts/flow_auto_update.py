"""KiroCrew Flow — cron de auto-update.

Detecta o diretório de instalação do app via installed.json, executa
``git pull --rebase origin main`` e reinstala o cron com ``install-cron.sh``.

Comportamento:
- Lê ``~/.kiro/crew/apps/kirocrew-flow/installed.json`` para obter o ``source``
  (caminho real do repositório clonado).
- Executa ``git pull --rebase origin main`` no diretório do repo.
- Executa ``./scripts/install-cron.sh`` para atualizar os scripts instalados.
- Se não houver atualização (git already up to date), encerra silenciosamente
  (``Skip()``).
- Se houver atualização, notifica via ``Report(msg)``.

Instalação: este script é copiado pelo ``scripts/install-cron.sh`` para
``~/.kiro/crew/crons/flow_auto_update.py``.

Registro (feito automaticamente via app.json ao instalar/habilitar o app):
    cron_add(name="flow-auto-update",
             script="~/.kiro/crew/crons/flow_auto_update.py:run",
             every=300)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)

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


def _find_repo_root() -> str | None:
    """Descobre o path do repositório via installed.json do app."""
    installed_json = os.path.expanduser(
        "~/.kiro/crew/apps/kirocrew-flow/installed.json"
    )
    if not os.path.exists(installed_json):
        logger.warning(
            "flow_auto_update: installed.json não encontrado em %s", installed_json
        )
        return None
    try:
        with open(installed_json, encoding="utf-8") as f:
            data = json.load(f)
        source = data.get("source", "")
        if source and os.path.isdir(source):
            return source
        logger.warning(
            "flow_auto_update: campo 'source' ausente ou inválido em installed.json: %r",
            source,
        )
    except Exception as exc:
        logger.warning("flow_auto_update: erro ao ler installed.json: %s", exc)
    return None


def run(ctx: object) -> None:
    """Entrypoint do cron de auto-update.

    Puxa atualizações do repo e reinstala os scripts de cron.
    Usa ``Skip`` quando já está atualizado, ``Report`` quando atualiza com sucesso.
    """
    repo_root = _find_repo_root()
    if repo_root is None:
        logger.error(
            "flow_auto_update: não foi possível determinar o diretório do repo — "
            "verifique installed.json em ~/.kiro/crew/apps/kirocrew-flow/"
        )
        raise Skip()

    # --- git pull --rebase origin main ---
    try:
        git_result = subprocess.run(
            ["git", "pull", "--rebase", "origin", "main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.warning("flow_auto_update: git pull expirou após 60s")
        raise Skip()
    except Exception as exc:
        logger.warning("flow_auto_update: erro ao executar git pull: %s", exc)
        raise Skip()

    if git_result.returncode != 0:
        logger.warning(
            "flow_auto_update: git pull falhou (rc=%d): %s",
            git_result.returncode,
            git_result.stderr.strip(),
        )
        raise Skip()

    already_up_to_date = "already up to date" in git_result.stdout.lower()
    if already_up_to_date:
        # Nenhuma atualização — não notifica, encerra silenciosamente.
        raise Skip()

    # --- scripts/install-cron.sh ---
    install_sh = os.path.join(repo_root, "scripts", "install-cron.sh")
    if not os.path.isfile(install_sh):
        logger.warning(
            "flow_auto_update: install-cron.sh não encontrado em %s", install_sh
        )
        raise Report(
            "⚠️ KiroCrew Flow atualizado mas install-cron.sh não encontrado "
            f"em {install_sh} — rode manualmente: ./scripts/install-cron.sh"
        )

    try:
        install_result = subprocess.run(
            ["bash", install_sh],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("flow_auto_update: install-cron.sh expirou após 120s")
        raise Report(
            "⚠️ KiroCrew Flow atualizado mas install-cron.sh expirou — "
            "rode manualmente: ./scripts/install-cron.sh"
        )
    except Exception as exc:
        logger.warning("flow_auto_update: erro ao executar install-cron.sh: %s", exc)
        raise Report(
            f"⚠️ KiroCrew Flow atualizado mas install-cron.sh falhou: {exc}"
        )

    if install_result.returncode != 0:
        logger.warning(
            "flow_auto_update: install-cron.sh falhou (rc=%d): %s",
            install_result.returncode,
            install_result.stderr.strip(),
        )
        raise Report(
            f"⚠️ KiroCrew Flow atualizado (git pull OK) mas install-cron.sh "
            f"falhou (rc={install_result.returncode}) — rode manualmente."
        )

    # Resumo do git pull (primeira linha não-vazia do stdout)
    summary = next(
        (line.strip() for line in git_result.stdout.splitlines() if line.strip()),
        "atualizado",
    )
    raise Report(f"✅ KiroCrew Flow auto-atualizado: {summary}")
