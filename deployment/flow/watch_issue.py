"""KiroCrew Flow — monitor zero-token de uma issue despachada.

Este script roda como cron SEM LLM (Python puro, só ``subprocess`` chamando
``gh``). Ele é criado automaticamente pelo cron de dev (``flow-develop-waiting``)
como efeito colateral do dispatch — ver ``_create_issue_monitor`` em
``deployment.py``.

Ciclo de vida:
    - Enquanto a sessão implementa e ainda não abriu PR: silêncio (``Skip``).
    - Quando a PR abre: notifica UMA vez (anti-spam por marcador em disco) e
      continua monitorando (``Skip``) até o merge.
    - Quando a issue fecha (PR mergeada): notifica e se auto-remove (``Done``).
    - Se não há PR e a sessão ficou sem atividade > 40min: notifica possível
      travamento e se auto-remove (``Done``).

Registro (feito automaticamente pelo executor):
    cron_add(name="watch-<repo_short>-<N>",
             script="~/.kiro/crew/crons/deployment/flow/watch_issue.py:check",
             message="owner/repo#N",
             every=180)

O ``ctx.message`` carrega o alvo no formato ``owner/repo#N``
(ex: ``eliasrosa/kirocrew-flow#211``).
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time

# ── Skip/Done/Report ──────────────────────────────────────────────────────
# O cron runner injeta essas exceções, mas import explícito deixa o script
# testável e legível. Fallback defensivo caso o módulo mude de lugar.
try:  # pragma: no cover - caminho de runtime
    from kiro_crew.cron_script import Done, Skip  # type: ignore[import]
except ImportError:  # pragma: no cover - fallback para teste isolado

    class Skip(Exception):  # type: ignore[no-redef]
        """Retenta no próximo tick (mantém o job)."""

        def __init__(self, message: str = "") -> None:
            self.message = message
            super().__init__(message)

    class Done(Exception):  # type: ignore[no-redef]
        """Entrega a mensagem e REMOVE o job."""

        def __init__(self, message: str = "") -> None:
            self.message = message
            super().__init__(message)


# Sessão sem atividade por mais que isto (segundos) = possível travamento.
_DEAD_SESSION_SECS = 2400  # 40 min

# Diretório de marcadores anti-spam (evita notificar "PR aberta" 2x).
_MARKER_DIR = os.path.expanduser("~/.kiro/crew/crons/.flow-watch")


def _gh_json(args: list[str]) -> object:
    """Roda ``gh`` e retorna o JSON parseado. Levanta em erro/output vazio."""
    out = subprocess.check_output(["gh", *args], text=True)
    return json.loads(out)


def _parse_target(message: str) -> tuple[str, str]:
    """Extrai ``(repo, number)`` de ``owner/repo#N``.

    Levanta ``ValueError`` se o formato não bater — o job então falha o tick
    (não se auto-remove), o que é o comportamento correto para um alvo malformado.
    """
    if "#" not in message:
        raise ValueError(f"message inválida (esperado 'owner/repo#N'): {message!r}")
    repo, _, number = message.partition("#")
    repo = repo.strip()
    number = number.strip()
    if not repo or not number.isdigit():
        raise ValueError(f"message inválida (esperado 'owner/repo#N'): {message!r}")
    return repo, number


def _marker_path(repo: str, number: str) -> str:
    """Caminho do marcador anti-spam para a PR desta issue."""
    short = repo.replace("/", "-")
    return os.path.join(_MARKER_DIR, f"{short}-{number}.pr-notified")


def _already_notified_pr(repo: str, number: str) -> bool:
    return os.path.exists(_marker_path(repo, number))


def _mark_pr_notified(repo: str, number: str) -> None:
    """Grava o marcador anti-spam (best-effort — nunca quebra o ciclo)."""
    with contextlib.suppress(OSError):
        os.makedirs(_MARKER_DIR, exist_ok=True)
        with open(_marker_path(repo, number), "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))


def _clear_marker(repo: str, number: str) -> None:
    """Remove o marcador ao encerrar o monitor (limpeza best-effort)."""
    with contextlib.suppress(OSError):
        os.remove(_marker_path(repo, number))


def _session_file(repo: str, number: str) -> str:
    """Caminho do ``.jsonl`` da sessão one-shot de dev desta issue."""
    short = repo.replace("/", "-")
    return os.path.expanduser(
        f"~/.kiro/crew/sessions/dashboard_esteira-{short}-{number}.jsonl"
    )


def _session_idle_secs(repo: str, number: str) -> float | None:
    """Segundos desde a última atividade da sessão, ou None se não existe."""
    path = _session_file(repo, number)
    if not os.path.exists(path):
        return None
    return time.time() - os.path.getmtime(path)


def check(ctx: object) -> None:
    """Entrypoint do cron zero-token. Ver docstring do módulo.

    ``ctx.message`` = ``"owner/repo#N"``.
    """
    repo, number = _parse_target(getattr(ctx, "message", ""))

    # 1. Estado da issue
    issue = _gh_json(
        ["issue", "view", number, "--repo", repo, "--json", "state,labels"]
    )
    assert isinstance(issue, dict)

    # 2. PRs da branch canônica feat/issue-N (qualquer estado)
    prs = _gh_json(
        [
            "pr", "list", "--repo", repo,
            "--head", f"feat/issue-{number}",
            "--state", "all",
            "--json", "number,state,url",
        ]
    )
    assert isinstance(prs, list)

    # 3. Issue fechada → PR mergeada (ou fechada manualmente). Encerra o monitor.
    if str(issue.get("state", "")).upper() == "CLOSED":
        pr_num = prs[0]["number"] if prs else "?"
        _clear_marker(repo, number)
        ctx.notify(f"🎉 issue #{number} fechou (PR #{pr_num} mergeada)")  # type: ignore[attr-defined]
        raise Done("issue fechada")

    # 4. PR aberta → notifica UMA vez, segue monitorando até o merge.
    open_prs = [p for p in prs if str(p.get("state", "")).upper() == "OPEN"]
    if open_prs:
        pr = open_prs[0]
        if not _already_notified_pr(repo, number):
            ctx.notify(  # type: ignore[attr-defined]
                f"✅ issue #{number} abriu PR #{pr['number']}\n{pr['url']}"
            )
            _mark_pr_notified(repo, number)
        raise Skip()  # continua até fechar/mergear

    # 5. Sem PR ainda → detecta sessão morta pelo mtime do .jsonl.
    idle = _session_idle_secs(repo, number)
    if idle is not None and idle > _DEAD_SESSION_SECS:
        _clear_marker(repo, number)
        ctx.notify(  # type: ignore[attr-defined]
            f"⚠️ issue #{number} pode ter travado "
            f"(sem PR após {int(idle // 60)}min sem atividade da sessão)"
        )
        raise Done("sessão morta detectada")

    # 6. Ainda implementando — silêncio.
    raise Skip()
