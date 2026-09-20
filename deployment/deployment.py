"""KiroCrew Flow — cron de scan e dispatch one-shot.

Cron de SCRIPT do Kiro Crew (sem LLM, zero token no polling).

  - Fase 1 (auto_dispatch=false): só AVISA — você aciona manual.
  - Fase 2 (auto_dispatch=true ): dispara sessão ONE-SHOT que implementa e abre PR.

Agora usa a arquitetura hexagonal de flow/:
  scan_candidates() ← flow/scan/scanner.py  → filtra issues candidatas (zero token)
  provider_for()    ← flow/ports/           → adapter GitHub ou Jira
  can_leave_spec()  ← flow/domain/gates.py → validação GATE 1 (zero token)

O dispatch (chamada de sessão one-shot) ainda vive aqui — é o driving adapter da Fase 1.

Depende do Kiro Crew rodando (loopback interno). NÃO é standalone.

## Entrypoints por estágio (recomendado)

Em vez de um único cron monolítico, a esteira pode ser dividida em 4 crons
independentes, cada um responsável por um estágio do fluxo:

    run_dev(ctx)       — issues crewflow:todo → dispatch dev (modelo mais forte)
    run_reviewer(ctx)  — PRs crewflow:review → dispatch reviewer (modelo mais rápido)
    run_merge(ctx)     — crewflow:review-ok → merge squash
    run_conflito(ctx)  — crewflow:review-fail → dispatch rework

Vantagens:
  - Observabilidade: cada cron tem log/histórico isolado
  - Modelo por ação: cada estágio pode usar um modelo diferente via `stage_models`
  - Blast radius menor: se merge quebra, dev/reviewer seguem
  - Interval por estágio: reviewer pode varrer mais rápido que dev

Registro (uma vez por estágio):
    cron_add(name="crewflow-dev",       script="~/.kiro/crew/crons/deployment.py:run_dev",       every=600)
    cron_add(name="crewflow-reviewer",  script="~/.kiro/crew/crons/deployment.py:run_reviewer",  every=300)
    cron_add(name="crewflow-merge",     script="~/.kiro/crew/crons/deployment.py:run_merge",     every=120)
    cron_add(name="crewflow-conflito",  script="~/.kiro/crew/crons/deployment.py:run_conflito",  every=300)

O entrypoint legado `run(ctx)` ainda funciona e orquestra todos os estágios em
sequência — útil em modo de aviso (auto_dispatch=false) ou durante a migração.

Config: ~/.kiro/crew/crons/deployment.config.yaml (copie de config.example.yaml).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading as _threading

logger = logging.getLogger(__name__)

# ── Adiciona o diretório raiz do repo ao path para importar flow/ ─────────
# Necessário porque o cron do Kiro Crew executa o arquivo diretamente e
# flow/ não está instalado como pacote no Python do sistema.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from datetime import UTC  # noqa: E402

# ── Detecção de script instalado desatualizado ────────────────────────────

def _check_installed_version(ctx: object | None = None) -> None:
    """Avisa quando o script instalado diverge da versão no repositório.

    Compara o hash SHA-256 do arquivo do repo (deployment/deployment.py) com
    o hash registrado em deployment.version no momento da última instalação
    via scripts/install-cron.sh.

    Quando divergir: loga um warning claro e — se ctx disponível —
    envia notificação pedindo reinstalação. Nunca aborta o ciclo (fail-open
    para a verificação de versão, fail-closed só para PromptRenderError).

    O arquivo deployment.version é criado pelo install-cron.sh e contém:
        {
          "repo_root": "/caminho/para/o/repo",
          "repo_deployment_sha256": "<sha256 de deployment.py no repo>"
        }

    Se o arquivo não existir (instalação antiga antes desta feature), apenas
    loga um aviso de que a verificação não está disponível — não bloqueia.
    """
    import hashlib

    version_file = os.path.join(_HERE, "deployment.version")
    if not os.path.exists(version_file):
        logger.debug(
            "deployment: deployment.version não encontrado — "
            "reinstale com scripts/install-cron.sh para habilitar verificação de versão"
        )
        return

    try:
        with open(version_file) as f:
            version_info = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "deployment: não foi possível ler deployment.version: %s — "
            "execute scripts/install-cron.sh para corrigir",
            exc,
        )
        return

    repo_root = version_info.get("repo_root") or ""
    expected_sha = version_info.get("repo_deployment_sha256") or ""

    if not repo_root or not expected_sha:
        logger.warning(
            "deployment: deployment.version incompleto — "
            "reinstale com scripts/install-cron.sh"
        )
        return

    repo_deployment = os.path.join(repo_root, "deployment", "deployment.py")
    if not os.path.exists(repo_deployment):
        logger.debug(
            "deployment: deployment.py do repo não encontrado em %s — "
            "verificação de versão pulada (repo movido?)",
            repo_deployment,
        )
        return

    try:
        with open(repo_deployment, "rb") as f:
            current_sha = hashlib.sha256(f.read()).hexdigest()
    except OSError as exc:
        logger.warning(
            "deployment: não foi possível ler deployment.py do repo (%s): %s — "
            "verificação de versão pulada",
            repo_deployment, exc,
        )
        return

    if current_sha == expected_sha:
        logger.debug("deployment: versão do script instalado OK (sha256 bate)")
        return

    msg = (
        "⚠️ KiroCrew Flow: script instalado DESATUALIZADO.\n"
        "  O deployment.py no repositório foi modificado após a última instalação.\n"
        f"  Hash instalado: {expected_sha[:12]}...\n"
        f"  Hash no repo:   {current_sha[:12]}...\n"
        "  Execute: ./scripts/install-cron.sh\n"
        f"  Repo: {repo_root}"
    )
    logger.warning(msg)

    if ctx is not None:
        try:
            ctx.notify(msg)  # type: ignore[attr-defined]
        except Exception as exc_notify:
            logger.debug("deployment: falha ao notificar versão desatualizada: %s", exc_notify)


from flow.audit.state_comment import (  # noqa: E402
    render_pr_review_comment,
)
from flow.domain.state import State, transition_state  # noqa: E402
from flow.ports.issue_provider import provider_for  # noqa: E402
from flow.prompts.loader import PromptRenderError, render_prompt  # noqa: E402
from flow.scan.cache import (  # noqa: E402
    clear_running_since,
    get_running_since,
    open_cache,
    set_running_since,
)
from flow.scan.scanner import ScanResult, scan_candidates  # noqa: E402

# ── Labels (mantidas para o prompt de dispatch) ───────────────────────────
LABEL_DEV     = "crewflow:dev"
LABEL_REVIEW  = "crewflow:review"
LABEL_RUNNING = "crewflow:running"
LABEL_BLOCKED = "crewflow:blocked"


# ── Helper de transição atômica de estado ─────────────────────────────────

def _apply_state_transition(
    current_labels: list[str] | set[str] | frozenset[str],
    new_state: State,
    add_modifiers: tuple[str, ...] = (),
    remove_modifiers: tuple[str, ...] = (),
) -> list[str]:
    """Aplica transição de estado de forma atômica via ``transition_state()``.

    Garante que exatamente 1 estado permaneça no resultado.  Modificadores e
    labels externas são preservados por ``transition_state()``; ``add_modifiers``
    e ``remove_modifiers`` permitem ajustes adicionais de modificadores em uma
    única operação.

    Args:
        current_labels:   Labels atuais da issue.
        new_state:        Estado de destino.
        add_modifiers:    Labels a adicionar (ex: ``("crewflow:running",)``).
        remove_modifiers: Labels a remover (ex: ``("crewflow:running",)``).

    Returns:
        Lista de labels resultante, pronta para passar a ``set_labels()``.
    """
    result = transition_state(frozenset(current_labels), new_state)
    result = result - frozenset(remove_modifiers)
    result = result | frozenset(m for m in add_modifiers if m)
    return sorted(result)  # sorted para determinismo nos testes


# ── Carregamento de config ────────────────────────────────────────────────
_CONFIG_CANDIDATES = [
    os.path.join(_HERE, "deployment.config.yaml"),
    os.path.expanduser("~/.kiro/crew/crons/deployment.config.yaml"),
]


def _load_config() -> dict:
    """Lê a config YAML (parser mínimo, sem dependência externa)."""
    path = next((p for p in _CONFIG_CANDIDATES if os.path.exists(p)), None)
    if not path:
        raise RuntimeError(
            "esteira: config não encontrada. Copie config.example.yaml para "
            "deployment.config.yaml ao lado do script."
        )
    try:
        import yaml  # type: ignore[import-untyped]
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return _mini_yaml(path)


def _mini_yaml(path: str) -> dict:
    """Parser YAML minimalista para quando PyYAML não está disponível.

    Delega para o parser compartilhado em ``flow.config.squad`` para que a
    config do cron e a config de squad tenham exatamente o mesmo suporte de
    schema (incluindo `routing:` na forma multi-linha com `match.labels`
    aninhado). Para squads com routing complexo, PyYAML continua recomendado
    (`pip install -e '.[yaml]'`).
    """
    from pathlib import Path

    from flow.config.squad import _mini_yaml as _shared_mini_yaml
    return dict(_shared_mini_yaml(Path(path)))


# ── Sessões ativas ────────────────────────────────────────────────────────

def _sessdir() -> str:
    return os.path.expanduser("~/.kiro/crew/sessions")


# Backstop anti-duplo-dispatch: lock válido apenas por poucos segundos.
# NÃO é o mecanismo principal de concorrência — só evita que dois ciclos
# consecutivos despachem a mesma issue antes de o primeiro ciclo ter marcado
# crewflow:running na API.
_DISPATCH_BACKSTOP_SECS = 120  # 2 minutos: tempo mínimo para o label aparecer na API


# Lock de thread para serializar a seção crítica de remoção de lock stale.
# O O_CREAT|O_EXCL é atômico entre processos, mas dois threads do mesmo
# processo podem passar simultaneamente pelo FileExistsError → is_stale=True →
# unlink: o segundo remove o arquivo que o primeiro acabou de criar, e ambos
# retornam True (TOCTOU, issue #141).  O threading.Lock serializa esta seção.
_dispatch_stale_lock = _threading.Lock()


def _try_acquire_dispatch_lock(repo: str, issue_number: int) -> tuple[bool, str]:
    """Tenta adquirir o backstop lock de forma atômica (O_CREAT|O_EXCL).

    Cria o arquivo de lock ANTES do POST /api/chat.  Se o arquivo já existe e
    ainda está dentro do período de backstop (_DISPATCH_BACKSTOP_SECS), a
    aquisição falha — sinal de que outro ciclo já fez o dispatch desta issue.

    Returns:
        (True, lock_path)  — lock adquirido; caller deve prosseguir com o dispatch.
        (False, lock_path) — lock já existia e ainda está válido; dispatch abortado.

    Implementação TOCTOU-free (issue #141):
    - Caminho normal: O_EXCL diretamente, sem pré-check exists().
    - Caminho stale: seção crítica protegida por threading.Lock para serializar
      o unlink + re-open e evitar que dois threads removam o arquivo um do outro.
      O threading.Lock é necessário porque O_EXCL é atômico entre processos mas
      dois threads do mesmo processo podem ambos passar pelo is_stale=True e
      fazer o unlink do arquivo que o outro acabou de criar.
    """
    short = repo.split("/")[-1]
    lock_path = os.path.join(
        _sessdir(), f"dashboard_esteira-{short}-{issue_number}.jsonl.lock"
    )

    def _try_open_excl() -> bool:
        """Tenta criar o lock com O_EXCL. Retorna True se adquiriu."""
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return True
        except FileExistsError:
            return False

    try:
        # Tentativa 1: caminho rápido (lock não existe)
        if _try_open_excl():
            return True, lock_path

        # Lock existe. Verificar se está válido ou stale.
        if not _lock_is_stale(lock_path):
            # Outro dispatch ativo dentro do backstop → abortar.
            return False, lock_path

        # Lock stale: seção crítica serializada por threading.Lock para evitar
        # que dois threads do mesmo processo façam unlink simultâneo.
        with _dispatch_stale_lock:
            # Re-checar dentro do lock: outro thread pode ter chegado aqui
            # primeiro e já ter criado um lock novo (não stale).
            if not _lock_is_stale(lock_path):
                return False, lock_path

            import contextlib
            with contextlib.suppress(OSError):
                os.unlink(lock_path)

            # Uma única tentativa atômica após o unlink.
            if _try_open_excl():
                return True, lock_path

        return False, lock_path

    except OSError:
        # Diretório não existe ou erro inesperado: fail-open para não bloquear
        # dispatch legítimo por problema de filesystem.
        logger.warning(
            "deployment: não foi possível criar lock atômico para %s#%s — "
            "prosseguindo sem backstop (diretório de sessões inacessível?)",
            repo, issue_number,
        )
        return True, lock_path

# Timeout de morte de sessão: quanto tempo uma issue pode ficar em
# crewflow:running sem sinais de vida antes de ser considerada morta.
# Deve ser maior que o tempo máximo de uma sessão legítima (~30min).
DEAD_SESSION_TIMEOUT_SECS = 40 * 60  # 40 minutos


def _lock_is_stale(path: str) -> bool:
    """Retorna True se o arquivo de lock existe mas expirou o backstop anti-duplo-dispatch."""
    try:
        age = __import__("time").time() - os.path.getmtime(path)
        return age > _DISPATCH_BACKSTOP_SECS
    except OSError:
        # Arquivo desapareceu entre o glob e a stat — trata como ausente.
        return True


def _active_sessions() -> int:
    """Retorna o número de sessões com backstop ativo (anti-duplo-dispatch).

    Mantido para uso como cap de concorrência em ``run()`` e ``_run_stage()``.
    A contagem é pelo backstop de locks (curto), não pelo timeout longo de morte.
    """
    locks = glob.glob(os.path.join(_sessdir(), "dashboard_esteira-*.jsonl.lock"))
    return sum(1 for p in locks if not _lock_is_stale(p))


def _repo_has_active(repo: str) -> bool:
    """Retorna True se há lock de backstop ativo para este repo.

    Usado APENAS como backstop anti-duplo-dispatch (curto).
    O mecanismo primário de concorrência é _issue_has_active_session().
    """
    short = repo.split("/")[-1]
    locks = glob.glob(os.path.join(_sessdir(), f"dashboard_esteira-{short}-*.jsonl.lock"))
    return any(not _lock_is_stale(p) for p in locks)


def _issue_has_active_session(
    repo: str,
    issue_number: int,
    dev_root: str,
) -> bool:
    """Verifica se a issue já tem uma sessão ativa — mecanismo primário de concorrência.

    Decisão baseada no ESTADO DA ISSUE, não em lock por tempo:
      1. Se não há label crewflow:running → issue não tem sessão ativa (deve ser despachada)
      2. Se há crewflow:running + PR aberto na branch feat/issue-N → sessão ativa (não redespachar)
      3. Se há crewflow:running + worktree existente → sessão ativa (não redespachar)
      4. Se há crewflow:running + backstop lock ativo → presumir ativa (aguardar expirar)
      5. Sem nenhum sinal → situação ambígua — fail-closed (não redespachar)

    NOTA: a ausência de crewflow:running no label da issue é o único sinal confiável
    de que a issue NÃO tem sessão ativa. Este método é chamado DEPOIS do scan, que
    já leu o estado atual da issue da API.
    """
    wt_path = _worktree_path(dev_root, repo, issue_number)

    # Sinal 1: worktree existente → sessão ativa
    if os.path.exists(wt_path):
        logger.debug(
            "concorrência: %s#%s — worktree existe em %s → ativa",
            repo, issue_number, wt_path,
        )
        return True

    # Sinal 2: PR aberto na branch da issue → sessão ativa
    if _pr_exists(repo, issue_number):
        logger.debug(
            "concorrência: %s#%s — PR aberto na branch feat/issue-%s → ativa",
            repo, issue_number, issue_number,
        )
        return True

    # Sinal 3: backstop lock ativo → provavelmente dispatch recente
    short = repo.split("/")[-1]
    locks = glob.glob(os.path.join(_sessdir(), f"dashboard_esteira-{short}-{issue_number}.jsonl.lock"))
    if any(not _lock_is_stale(p) for p in locks):
        logger.debug(
            "concorrência: %s#%s — backstop lock ativo → presume ativa (anti-duplo-dispatch)",
            repo, issue_number,
        )
        return True

    # Sem sinal confirmando sessão ativa.
    # Se crewflow:running estava presente (chamador verificou), pode ser sessão morta.
    # Retorna False para permitir que o detector de morte decida.
    return False


def _is_dead_session(
    repo: str,
    issue_number: int,
    dev_root: str,
    running_since_secs: float | None,
    dead_session_timeout: float = DEAD_SESSION_TIMEOUT_SECS,
) -> bool:
    """Detecta sessão morta — NÃO é liberador de fila, é detector de falha.

    Uma sessão é considerada MORTA quando TODOS estes sinais estão presentes:
      - crewflow:running está na issue (chamador verificou)
      - sem PR aberto na branch feat/issue-N
      - sem worktree ativo no caminho canônico
      - sem backstop lock ativo
      - running há mais de ``dead_session_timeout`` segundos

    Se algum desses sinais estiver ausente ou ambíguo → NÃO é morte confirmada
    (fail-closed). Melhor notificar o TL do que redespachar e abrir 2 PRs.

    Args:
        repo:                 ex "owner/repo"
        issue_number:         número da issue
        dev_root:             raiz dos clones (cfg["dev_root"])
        running_since_secs:   seconds desde epoch de quando running foi registrado.
                              None = desconhecido → fail-closed (retorna False)
        dead_session_timeout: timeout em segundos (default: DEAD_SESSION_TIMEOUT_SECS)
    """
    import time

    # Sem timestamp → não sabemos há quanto tempo está em running → fail-closed
    if running_since_secs is None:
        return False

    elapsed = time.time() - running_since_secs
    if elapsed < dead_session_timeout:
        logger.debug(
            "sessão %s#%s: running há %.0fs < timeout %ds → não é morte",
            repo, issue_number, elapsed, dead_session_timeout,
        )
        return False

    # Tempo expirou — verifica sinais de vida
    wt_path = _worktree_path(dev_root, repo, issue_number)
    if os.path.exists(wt_path):
        logger.info(
            "sessão %s#%s: running há %.0fs mas worktree existe → ainda ativa",
            repo, issue_number, elapsed,
        )
        return False

    if _pr_exists(repo, issue_number):
        logger.info(
            "sessão %s#%s: running há %.0fs mas PR aberto existe → ainda ativa",
            repo, issue_number, elapsed,
        )
        return False

    short = repo.split("/")[-1]
    locks = glob.glob(os.path.join(_sessdir(), f"dashboard_esteira-{short}-{issue_number}.jsonl.lock"))
    if any(not _lock_is_stale(p) for p in locks):
        logger.info(
            "sessão %s#%s: running há %.0fs mas backstop lock ativo → ainda ativa",
            repo, issue_number, elapsed,
        )
        return False

    # Todos os sinais indicam morte
    logger.warning(
        "sessão MORTA detectada: %s#%s — running há %.0fs, sem PR, sem worktree, sem lock",
        repo, issue_number, elapsed,
    )
    return True


def _recover_dead_session(
    ctx: object,
    repo: str,
    issue_number: int,
    provider: object,
    chat_id: str,
    conn: sqlite3.Connection,
) -> None:
    """Recupera issue com sessão morta: volta para crewflow:todo e notifica.

    Operação fail-safe: erro na recuperação é logado mas não propaga.
    Preferimos não redespachar automaticamente — apenas notificamos o TL
    para que ele decida. O redespacho ocorre no próximo ciclo quando o
    TL ou o cron ler o estado limpo (crewflow:todo sem running).
    """
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
    issue_url = f"https://github.com/{repo}/issues/{issue_number}"

    try:
        # Remove crewflow:running da issue via provider
        # (o provider é o objeto com .set_labels, .get_work_item etc.)
        _prov = provider  # type: ignore[assignment]
        item_data = _prov.get_work_item(repo, str(issue_number))  # type: ignore[attr-defined]
        current_labels = list(item_data.get("labels", []))
        if "crewflow:running" in current_labels:
            current_labels.remove("crewflow:running")
        _prov.set_labels(repo, str(issue_number), current_labels)  # type: ignore[attr-defined]

        # Limpa running_since no cache (import no topo do módulo)
        clear_running_since(conn, issue_url)

        logger.info(
            "deployment: sessão morta recuperada — %s#%s voltou para crewflow:todo",
            repo, issue_number,
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"⚠️ KiroCrew Flow: sessão morta detectada e recuperada.{vm}\n"
            f"  {repo}#{issue_number} foi encontrado com crewflow:running sem PR/worktree/lock.\n"
            f"  crewflow:running removido. Issue voltará para crewflow:todo no próximo ciclo.\n"
            f"  Verifique: {issue_url}"
        )
    except Exception as exc:
        logger.error(
            "deployment: falha ao recuperar sessão morta %s#%s: %s",
            repo, issue_number, exc,
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"⚠️ KiroCrew Flow: sessão morta detectada mas NÃO recuperada automaticamente.{vm}\n"
            f"  {repo}#{issue_number} — ação manual necessária.\n"
            f"  Erro: {exc}\n"
            f"  Issue: {issue_url}"
        )


# ── Workspace isolado por task (worktree efêmero) ─────────────────────────

def _worktree_path(dev_root: str, repo: str, issue_number: int) -> str:
    """Retorna o caminho canônico do worktree efêmero para esta task.

    Convenção: <dev_root>/.esteira-worktrees/<repo-short>-<issue_number>
    Usada tanto pelo deployment (limpeza pré-dispatch) quanto pelo prompt
    enviado à sessão one-shot, garantindo que ambos falem do mesmo diretório.
    """
    short = repo.split("/")[-1]
    return os.path.join(dev_root, ".esteira-worktrees", f"{short}-{issue_number}")


def _clean_stale_worktree(dev_root: str, repo: str, issue_number: int) -> bool:
    """Remove worktree órfão de uma execução anterior, se existir.

    Retorna True se havia worktree órfão e foi removido; False se não havia nada.
    O worktree é considerado órfão quando o diretório existe mas a sessão
    correspondente já não está ativa (lock inexistente ou stale).

    Usa ``git worktree remove --force`` para garantir que o índice do .git
    principal seja atualizado corretamente. Falhas são logadas mas não propagadas
    — um worktree preso não deve bloquear o dispatch de outras tasks.
    """
    wt_path = _worktree_path(dev_root, repo, issue_number)
    if not os.path.exists(wt_path):
        return False

    short = repo.split("/")[-1]
    base_repo = os.path.join(dev_root, short)

    logger.warning(
        "deployment: worktree órfão encontrado em %s — removendo antes do novo dispatch",
        wt_path,
    )
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", wt_path],
            cwd=base_repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        logger.info("deployment: worktree órfão removido: %s", wt_path)
        return True
    except subprocess.CalledProcessError as exc:
        logger.error(
            "deployment: falha ao remover worktree órfão %s: %s",
            wt_path, exc.stderr.strip(),
        )
        # Tenta remoção forçada via shutil como último recurso
        try:
            import shutil
            shutil.rmtree(wt_path, ignore_errors=True)
            # Limpa a referência do git mesmo que o rmtree tenha funcionado
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=base_repo,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            logger.warning(
                "deployment: worktree órfão %s removido via shutil (fallback)", wt_path
            )
            return True
        except Exception as exc2:
            logger.error(
                "deployment: não foi possível remover worktree órfão %s: %s",
                wt_path, exc2,
            )
            return False
    except Exception as exc:
        logger.error(
            "deployment: erro inesperado ao remover worktree %s: %s", wt_path, exc
        )
        return False


def _resource_headroom_ok(ctx: object, max_concurrent: int) -> bool:
    """Verifica se há headroom de recursos para despachar uma nova task.

    Consulta o endpoint de resource_status do Kiro Crew (se disponível).
    Retorna True quando o posture é 'ample' ou 'tight' E o número de sessões
    ativas está abaixo de max_concurrent. Retorna False quando 'critical'.

    Em caso de falha ao consultar (Kiro Crew não disponível, timeout, etc.),
    retorna True (fail-open): o cap de max_concurrent já age como barreira mínima.
    """
    try:
        import urllib.request as _u
        port = getattr(ctx, "_port", 5000)
        secret = getattr(ctx, "_secret", "")
        req = _u.Request(
            f"http://localhost:{port}/api/resource-status",
            headers={"X-Internal-Secret": secret},
            method="GET",
        )
        from kiro_crew.loopback_http import loopback_urlopen  # type: ignore[import]
        with loopback_urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        posture = data.get("posture", "ample")
        if posture == "critical":
            logger.warning(
                "deployment: resource_status=critical — dispatch suspenso até liberar recursos"
            )
            return False
        logger.debug("deployment: resource_status=%s — dispatch permitido", posture)
        return True
    except ImportError:
        # kiro_crew.loopback_http não disponível (teste unitário ou ambiente sem KC)
        return True
    except Exception as exc:
        logger.debug("deployment: resource_status indisponível (%s) — seguindo com dispatch", exc)
        return True


# ── Prompt de dispatch ────────────────────────────────────────────────────

# Fallback embutido para o prompt do dev — usado quando flow/prompts/dev.md
# não existe no disco (apagado, corrompido, ou deploy sem o diretório).
# É exatamente o conteúdo canônico que o template MD versiona.
_DEV_PROMPT_FALLBACK = (
    "# {{session_title}}\n\n"
    "## Agente\n\n"
    "| Campo | Valor |\n"
    "|-------|-------|\n"
    "| Repo | `{{repo}}` |\n"
    "| Issue | [#{{issue_number}}]({{issue_url}}) — {{issue_title}} |\n\n"
    "## Contexto da task\n\n"
    "Você é um agente de implementação ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.\n\n"
    "### Fluxo\n\n"
    "Execute UMA vez, do início ao fim, e PARE:\n\n"
    "1. CONTEXTO: leia TODA a documentação do repo antes de qualquer ação:\n"
    "   - `.kiro/steering/*.md` (steerings do projeto)\n"
    "   - `README.md`\n"
    "   - `docs/` se existir\n"
    "   - A própria issue: `gh issue view {{issue_number}} --repo {{repo}}`\n"
    "   - Os comentários da issue: `gh issue view {{issue_number}} --repo {{repo}} --comments`\n"
    "   Não pule esta etapa — as steerings têm convenções e gotchas críticos, e os\n"
    "   comentários podem conter adendos e decisões que refinam o escopo.\n"
    "2. ESCOPO: se a issue exige decisão de design não-tomada ou é vaga, NÃO implemente — "
    "comente, marque `crewflow:blocked`, avise e ENCERRE.\n"
    "3. Marque `crewflow:dev` + `crewflow:running` e REMOVA `crewflow:todo`. NÃO faça `git clone`. Use o clone em "
    "`{{dev_root}}/{{repo_short}}` como base e crie um WORKTREE ISOLADO.\n"
    "   A branch base é a DEFAULT DO REPO — descubra, não presuma:\n"
    "   `BASE=$(gh repo view {{repo}} --json defaultBranchRef --jq .defaultBranchRef.name)`\n"
    "   `cd {{dev_root}}/{{repo_short}} && git fetch origin && git worktree add -b "
    "feat/issue-{{issue_number}} {{worktree_path}} \"origin/$BASE\"`\n"
    "   Para trocar o estado, use SEMPRE a forma atômica que remove todos os estados anteriores:\n"
    "   `gh issue edit {{issue_number}} --repo {{repo}} --add-label \"crewflow:dev,crewflow:running\" --remove-label \"crewflow:todo\"`\n"
    "   Trabalhe DENTRO do worktree; remova-o ao fim. NUNCA toque em outros worktrees.\n"
    "4. Implemente EXATAMENTE o escopo — nada além.\n"
    "5. DOCS: atualize README, steerings e docs/ se a mudança afeta comportamento, "
    "arquitetura ou convenções. Não atualize se a mudança for puramente interna (bugfix, refactor).\n"
    "6. **VALIDAÇÃO OBRIGATÓRIA — rode ANTES de abrir PR.** Se o repo for `eliasrosa/kirocrew-flow`, execute exatamente:\n"
    "   ```bash\n"
    "   python3 -m ruff check flow/\n"
    "   python3 -m mypy flow/ --ignore-missing-imports\n"
    "   python3 -m pytest flow/tests/ --cov=flow --cov-fail-under=75\n"
    "   ```\n"
    "   Para outros repos, descubra os comandos via README/Makefile/pyproject — **não presuma**.\n"
    "   Se qualquer check falhar e você não conseguir corrigir, marque `crewflow:blocked` e ENCERRE. "
    "**Não abra PR com CI vermelho.**\n"
    "7. Abra PR com 'Closes #{{issue_number}}' e troque a label para `crewflow:review` REMOVENDO `crewflow:dev`. "
    "Após abrir o PR, ATUALIZE o título da sessão adicionando o número do PR: "
    "`{{repo_short}} #{{issue_number}} #<N-PR>: {{issue_title}}`. "
    "**NUNCA mergeie. NUNCA faça deploy.** Ambos são ações humanas manuais.\n"
    "   Use SEMPRE a forma atômica que remove todos os estados anteriores:\n"
    "   `gh issue edit {{issue_number}} --repo {{repo}} --add-label \"crewflow:review\" --remove-label \"crewflow:dev,crewflow:todo,crewflow:running\"`\n"
    "8. Ao terminar: {{notify_step}}\n\n"
    "   remova `crewflow:running` (mantenha `crewflow:review`), e ENCERRE.\n"
    "   `gh issue edit {{issue_number}} --repo {{repo}} --remove-label \"crewflow:running\"`\n\n"
    "{{vault_step}}\n\n"
    "### Regras críticas\n\n"
    "- UMA passada. Terminou, acabou. NÃO entre em loop.\n"
    "- NUNCA mergeie. NUNCA faça deploy.\n"
    "- Se bloquear, marque `crewflow:blocked`, avise, e pare.\n\n"
    "{{prompt_extra}}"
)


def _dispatch_prompt(
    repo: str,
    issue: dict,
    cfg: dict,
    prompt_extra: str = "",
) -> str:
    """Carrega e renderiza o template MD do estágio 'dev'.

    Usa ``flow/prompts/dev.md`` como fonte primária. Em caso de arquivo ausente
    ou corrompido, cai no fallback embutido ``_DEV_PROMPT_FALLBACK``.
    Variável faltando → ``PromptRenderError`` (fail-closed).
    """
    short = repo.split("/")[-1]
    vault = cfg.get("vault_root") or ""
    dev_root = cfg.get("dev_root") or os.path.expanduser("~/dev")
    chat_id = cfg.get("notify_chat_id") or ""

    vault_step = ""
    if vault:
        vault_step = (
            f"   - VAULT: edite `{vault}/Projetos/{short}/backlog.md` refletindo a issue "
            f"resolvida e sincronize com `sh {vault}/.sync.sh \"<msg>\"` (NUNCA `git push` "
            "literal). Se a pasta não existir, pule sem erro."
        )
    notify_step = (
        f"avise via voice_maybe (chat_id {chat_id}, intent auto) com TL;DR, "
        if chat_id else "reporte o resultado, "
    )
    worktree = _worktree_path(dev_root, repo, issue["number"])
    session_title = f"{short} #{issue['number']}: {issue['title']}"

    try:
        return render_prompt(
            "dev",
            fallback=_DEV_PROMPT_FALLBACK,
            repo=repo,
            repo_short=short,
            issue_number=str(issue["number"]),
            issue_title=issue["title"],
            issue_url=issue["url"],
            session_title=session_title,
            dev_root=dev_root,
            worktree_path=worktree,
            notify_step=notify_step,
            vault_step=vault_step,
            prompt_extra=prompt_extra.strip(),
        )
    except PromptRenderError:
        logger.exception(
            "deployment: erro ao renderizar template 'dev' para %s#%s — dispatch abortado",
            repo, issue["number"],
        )
        raise


def _pr_exists(repo: str, issue_number: int) -> bool:
    """Retorna True se já existe um PR aberto para a issue N neste repo.

    Previne que a sessão one-shot abra um segundo PR quando a primeira branch
    já está em review — inclusive quando a branch tem nome alternativo (não segue
    o padrão ``feat/issue-N``).

    Estratégia dupla (rede de segurança):
    1. Busca pelo nome canônico da branch (``--head feat/issue-N``) — rápido e
       preciso quando o padrão é seguido.
    2. Busca por referência à issue no corpo do PR (``--search "Closes #N in:body"``
       ou ``"Fixes #N in:body"``) — captura PRs com branch de nome alternativo.

    Retorna True se qualquer das duas buscas encontrar ao menos uma PR aberta.
    """
    branch = f"feat/issue-{issue_number}"

    def _run_gh_pr_list(extra_args: list[str]) -> list[dict]:
        """Executa gh pr list com os args fornecidos e retorna a lista de PRs."""
        cmd = ["gh", "pr", "list", "--repo", repo, "--state", "open",
               "--json", "number", *extra_args]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15, check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "deployment: gh pr list falhou para %s (%s): %s",
                    repo, " ".join(extra_args), result.stderr.strip(),
                )
                return []
            return json.loads(result.stdout or "[]")
        except Exception as exc:
            logger.warning(
                "deployment: erro em gh pr list para %s (%s): %s",
                repo, " ".join(extra_args), exc,
            )
            return []

    # 1ª busca: nome canônico da branch
    prs_by_branch = _run_gh_pr_list(["--head", branch])
    if prs_by_branch:
        logger.info(
            "deployment: PR já existe para %s#%s (branch %s) — dispatch ignorado",
            repo, issue_number, branch,
        )
        return True

    # 2ª busca: referência à issue no corpo da PR (branch de nome alternativo)
    search_query = f"Closes #{issue_number} in:body"
    prs_by_body = _run_gh_pr_list(["--search", search_query])
    if prs_by_body:
        pr_numbers = [p.get("number") for p in prs_by_body]
        logger.info(
            "deployment: PR já existe para %s#%s (branch alternativa, PRs=%s) — dispatch ignorado",
            repo, issue_number, pr_numbers,
        )
        return True

    return False


def _dispatch(
    ctx: object,
    repo: str,
    issue: dict,
    cfg: dict,
    prompt_extra: str = "",
) -> None:
    """Fire-and-forget POST /api/chat (loopback interno).

    Adquire o backstop lock de forma ATÔMICA (O_CREAT|O_EXCL) ANTES de fazer
    o POST /api/chat.  Dois ciclos concorrentes que chegarem aqui ao mesmo
    tempo para a mesma issue: apenas o primeiro obtém o lock e prossegue; o
    segundo aborta silenciosamente.  Isso fecha a janela de race entre o
    POST e o momento em que a sessão spawnada deixa rastro (worktree, label).
    """
    import urllib.request as _u

    # ── Guard: issue CLOSED → não despachar (fix #163) ───────────────────
    # Cobre a race onde a PR canônica mergeia (fechando a issue via "Closes #N")
    # enquanto o cron ainda vê crewflow:todo na cache de labels.  O state da
    # issue via API já retorna CLOSED imediatamente — mais confiável que aguardar
    # a propagação do label crewflow:done.
    if _is_issue_closed(repo, issue["number"]):
        logger.info(
            "deployment: _dispatch abortado — issue %s#%s está CLOSED (guard #163)",
            repo, issue["number"],
        )
        return

    # ── Reserva atômica: ANTES do POST ───────────────────────────────────
    acquired, _lock_path = _try_acquire_dispatch_lock(repo, issue["number"])
    if not acquired:
        logger.info(
            "deployment: _dispatch abortado — backstop lock já existe para %s#%s "
            "(outro ciclo despachou primeiro)",
            repo, issue["number"],
        )
        return

    slot = f"esteira-{repo.split('/')[-1]}-{issue['number']}"
    try:
        message = _dispatch_prompt(repo, issue, cfg, prompt_extra=prompt_extra)
    except PromptRenderError as exc:
        logger.error(
            "deployment: _dispatch abortado — template 'dev' inválido para %s#%s: %s",
            repo, issue["number"], exc,
        )
        return
    body = json.dumps({
        "message": message,
        "agent": cfg.get("agent") or "kirocrew",
        "slot": slot,
        "memory_mode": "temporary",
    }).encode()
    req = _u.Request(
        f"http://localhost:{ctx._port}/api/chat",  # type: ignore[attr-defined]
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": ctx._secret,  # type: ignore[attr-defined]
            "X-Session-Key": f"cron:{ctx.job.id}",  # type: ignore[attr-defined]
        },
        method="POST",
    )
    try:
        from kiro_crew.loopback_http import loopback_urlopen  # type: ignore[import]
        with loopback_urlopen(req, timeout=3) as resp:
            resp.read(1)
    except Exception:
        pass


# ── Conversão ScanResult → formato legado do dispatch ────────────────────

def _scan_result_to_issue(result: object) -> dict:
    """Converte um ScanResult para o formato mínimo que o prompt de dispatch precisa."""
    r: ScanResult = result  # type: ignore[assignment]
    item = r.item
    # Extrai o número da issue key ("VGAT-123" → 123, "owner/repo#42" → 42)
    import re as _re
    m = _re.search(r"[#\-/](\d+)$", item.key)
    number = int(m.group(1)) if m else 0
    return {
        "number": number,
        "title": item.title,
        "url": item.key,  # key é a URL canônica no adapter GitHub
    }


# ── Ponto de entrada do cron ──────────────────────────────────────────────

def _dry_run_report(
    scan_results: list,
    dispatch_devs: list,
    dispatch_reviewers: list,
    dispatch_reworks: list,
    needs_human: list,
    blocked_bypass: list,
    rebranded: list,
    merge_prs: list,
    spec_invalid: list,
    conflict_resolvers: list | None = None,
    mark_conflitos: list | None = None,
) -> None:
    """Imprime o relatório de dry-run no stdout sem executar nenhum efeito colateral."""
    _conflict_resolvers = conflict_resolvers or []
    _mark_conflitos = mark_conflitos or []

    print("[DRY-RUN] ──────────────────────────────────────────")
    print(f"[DRY-RUN] {len(scan_results)} issue(s) processada(s) pelo scan")
    print("[DRY-RUN] Decisões (nenhuma será executada):")
    print()

    for repo, issue, decision in dispatch_devs:
        print(f"[DRY-RUN] {repo}#{issue['number']} → DISPATCH_DEV (template via executor) — {issue['title']}")

    for repo, issue in dispatch_reviewers:
        print(f"[DRY-RUN] {repo}#{issue['number']} → DISPATCH_REVIEWER — {issue['title']}")

    for repo, issue, _sc in dispatch_reworks:
        print(f"[DRY-RUN] {repo}#{issue['number']} → DISPATCH_REWORK — {issue['title']}")

    for repo, issue in _conflict_resolvers:
        print(f"[DRY-RUN] {repo}#{issue['number']} → DISPATCH_CONFLICT_RESOLVER — {issue['title']}")

    for result in _mark_conflitos:
        _r_mc: ScanResult = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {_r_mc.item.key} → MARK_CONFLITO — {_r_mc.item.title}")

    for repo, issue in merge_prs:
        print(f"[DRY-RUN] {repo}#{issue['number']} → MERGE_PR — {issue['title']}")

    for result, decision, _sc in needs_human:
        r: ScanResult = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {r.item.key} → NOTIFY_HUMAN {getattr(getattr(decision, 'notify_role', None), 'value', '?')} ({r.item.title})")

    for result, decision in rebranded:
        r = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {r.item.key} → REBRAND → {getattr(decision, 'new_template', '?')} ({r.item.title})")

    for result in blocked_bypass:
        r = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {r.item.key} → BLOCK (hml-bypass sem justificativa) — {r.item.title}")

    for result in spec_invalid:
        r = result  # type: ignore[assignment]
        print(f"[DRY-RUN] {r.item.key} → SPEC_INVALID (sem repo no título) — {r.item.title}")

    total_actions = (
        len(dispatch_devs) + len(dispatch_reviewers) + len(dispatch_reworks) + len(merge_prs)
        + len(needs_human) + len(rebranded) + len(blocked_bypass) + len(spec_invalid)
    )
    skipped = max(0, len(scan_results) - total_actions)
    if skipped > 0:
        print(f"[DRY-RUN] {skipped} issue(s) sem ação (SKIP)")

    print()
    print("[DRY-RUN] ── Nenhuma sessão despachada, label alterada ou notificação enviada. ──")


def _log_cycle_summary(
    ctx: object,
    chat_id: str,
    scan_total: int,
    dispatch_dev: int,
    dispatch_reviewer: int,
    dispatch_rework: int,
    merge_pr: int,
    notify_human: int,
    block: int,
    rebrand: int,
    spec_invalid: int,
    conflict_resolver: int = 0,
    mark_conflito: int = 0,
) -> None:
    """Emite 1 linha de resumo do ciclo no log e no ctx.notify() quando configurado."""
    total_actions = (
        dispatch_dev + dispatch_reviewer + dispatch_rework + merge_pr
        + notify_human + block + rebrand + spec_invalid
        + conflict_resolver + mark_conflito
    )
    skipped = max(0, scan_total - total_actions)
    summary = (
        f"deployment: ciclo concluído — "
        f"scan:{scan_total} "
        f"dispatch_dev:{dispatch_dev} "
        f"dispatch_reviewer:{dispatch_reviewer} "
        f"dispatch_rework:{dispatch_rework} "
        f"conflict_resolver:{conflict_resolver} "
        f"mark_conflito:{mark_conflito} "
        f"merge_pr:{merge_pr} "
        f"notify_human:{notify_human} "
        f"block:{block} "
        f"rebrand:{rebrand} "
        f"skip:{skipped}"
    )
    logger.info(summary)
    if chat_id:
        ctx.notify(summary)  # type: ignore[attr-defined]


def run(ctx: object) -> None:
    _check_installed_version(ctx)
    cfg = _load_config()
    repos: list[str] = cfg.get("repos") or []
    auto = bool(cfg.get("auto_dispatch", False))
    # max_concurrent_tasks é o nome canônico (Fase 2); max_concurrent mantido para compat.
    max_conc = int(cfg.get("max_concurrent_tasks") or cfg.get("max_concurrent", 2))
    dev_root: str = cfg.get("dev_root") or os.path.expanduser("~/dev")
    chat_id = cfg.get("notify_chat_id") or ""
    issue_provider_name: str = cfg.get("issue_provider", "github")

    # ── Modo dry-run: inspeciona o que seria feito sem executar efeitos ──
    # Ativado por CREWFLOW_DRY_RUN=1 (variável de ambiente) ou dry_run: true na config.
    dry_run = bool(os.environ.get("CREWFLOW_DRY_RUN")) or bool(cfg.get("dry_run", False))
    if dry_run:
        logger.info("deployment: modo dry-run ativado — nenhuma ação será executada")

    if not repos:
        logger.warning("deployment: nenhum repo/projeto configurado")
        return

    # ── Carrega a SquadConfig: arquivo squads/<id>.yaml > inline da config ─
    from flow.config.squad import SquadConfig, SquadConfigError, load_squad

    squad: SquadConfig | None = None
    squad_file = cfg.get("squad_config")   # caminho opcional na config
    if squad_file and not os.path.exists(squad_file):
        raise RuntimeError(
            f"deployment: squad_config aponta para um arquivo que não existe: {squad_file!r}"
        )
    if squad_file and os.path.exists(squad_file):
        try:
            squad = load_squad(squad_file)
            logger.info("deployment: squad carregada de %s (%s)", squad_file, squad.id)
        except SquadConfigError as exc:
            logger.warning("deployment: falha ao carregar squad config: %s", exc)

    if squad is None:
        # Constrói inline a partir da config legada
        from flow.config.squad import _parse_squad
        raw: dict = {
            "id": cfg.get("squad_id", "default"),
            "issue_provider": issue_provider_name,
            "repos": repos,
            "workflow_template": cfg.get("workflow_template", "versao-c"),
        }
        if cfg.get("project"):
            raw["project"] = cfg["project"]
        raw_params = cfg.get("workflow_params") or {}
        if raw_params:
            raw["workflow_params"] = raw_params
        raw_routing = cfg.get("routing") or []
        if raw_routing:
            raw["routing"] = raw_routing
        try:
            squad = _parse_squad(raw)
            logger.debug("deployment: squad construída inline (id=%s)", squad.id)
        except SquadConfigError as exc:
            logger.error("deployment: squad config inválida: %s", exc)

    # ── Inicializa o provider e o cache ────────────────────────────────────
    provider = provider_for(issue_provider_name)

    # Usa SquadScanConfig como adaptador entre SquadConfig e scan_candidates
    # (scan_candidates aceita qualquer objeto com squad_id, issue_provider,
    #  projects e repos — SquadConfig satisfaz essa interface)
    if squad is not None:
        from flow.scan.scanner import SquadScanConfig
        scan_cfg = SquadScanConfig(
            squad_id=squad.id,
            issue_provider=squad.issue_provider,
            projects=tuple(squad.projects),
            repos=squad.repos,
        )
    else:
        from flow.scan.scanner import SquadScanConfig
        scan_cfg = SquadScanConfig(
            squad_id=cfg.get("squad_id", "default"),
            issue_provider=issue_provider_name,
            projects=tuple(repos),
            repos=frozenset(repos),
        )

    conn: sqlite3.Connection = open_cache(scan_cfg.squad_id)

    # ── Executa o scan zero-token ─────────────────────────────────────────
    try:
        scan_results = scan_candidates(scan_cfg, provider, conn)
    except Exception as exc:
        logger.error("deployment: erro no scan: %s", exc)
        conn.close()
        from kiro_crew.cron import Skip  # type: ignore[import]
        raise Skip() from exc

    # ── Rastreia running_since no cache (detecção de sessão morta) ────────
    # Para cada issue com crewflow:running: registra quando foi visto pela 1ª vez.
    # Para issues sem crewflow:running: limpa o timestamp (issue saiu do estado running).
    from datetime import UTC

    from flow.domain.state import Modifier as _Modifier
    _now_iso = __import__("datetime").datetime.now(tz=UTC).isoformat()
    for _r in scan_results:
        if _Modifier.RUNNING in _r.modifiers:
            set_running_since(conn, _r.item.key, _now_iso)
        else:
            clear_running_since(conn, _r.item.key)

    conn.close()

    # ── Separa candidatos de dispatch dos informativos ────────────────────
    # ── Passa todos os resultados pelo executor ────────────────────────────
    from flow.executor.executor import ActionKind, decide, resolve_template

    # Categorias de resultado após o executor — tipadas para mypy
    spec_invalid: list[ScanResult] = []
    dispatch_devs: list[tuple[str, dict, object]] = []   # (repo, issue, decision)
    dispatch_reviewers: list[tuple[str, dict]] = []      # (repo, issue)
    dispatch_reworks: list[tuple[str, dict, str | None]] = []  # (repo, issue, state_comment)
    conflict_resolvers: list[tuple[str, dict]] = []      # (repo, issue) — cron de conflito
    mark_conflitos: list[ScanResult] = []                # issues para marcar crewflow:conflito
    needs_human: list[tuple[ScanResult, object, str | None]] = []  # (result, decision, sc)
    blocked_bypass: list[ScanResult] = []                # result com bypass sem justif
    rebranded: list[tuple[ScanResult, object]] = []      # (result, decision)
    merge_prs: list[tuple[str, dict, str | None]] = []   # (repo, issue, state_comment) — merge squash automático
    dead_session_candidates: list[ScanResult] = []       # issues dev+running sem sinais de vida

    # Conjunto de issues em crewflow:dev + crewflow:running: usadas para calcular
    # o cap de concorrência por estado (sem depender de locks de arquivo).
    _running_dev_count = sum(
        1 for _r in scan_results
        if _r.current_state is not None
        and _r.current_state.value == "crewflow:dev"
        and _Modifier.RUNNING in _r.modifiers
    )

    for result in scan_results:
        # Flags do scan que não precisam do executor
        if result.spec_valid is False:
            spec_invalid.append(result)
            continue

        # Passa pelo executor para decisão completa
        # Lê o comentário de estado se precisar (bypass, COV) — só faz a
        # chamada de I/O quando o estado pode precisar dele
        from flow.domain.state import Modifier, State
        needs_comment = (
            Modifier.HML_BYPASS in result.modifiers
            or (hasattr(result.current_state, "__eq__") and result.current_state is State.DEV)
            or (result.current_state is State.TODO and "crewflow:debt" in result.item.labels)
            # GATE 2: lê o resultado do reviewer quando em review+reviewed
            or (result.current_state is State.REVIEW and Modifier.REVIEWED in result.modifiers)
            # Ciclo de re-trabalho: lê iterações para checar teto
            or (Modifier.CHANGES_REQUESTED in result.modifiers)
        )
        state_comment: str | None = None
        if needs_comment:
            import contextlib
            with contextlib.suppress(Exception):
                state_comment = provider.get_state_comment(
                    result.item.key.split("/issues/")[0].replace("https://github.com/", "")
                    or (repos[0] if repos else ""),
                    result.item.key,
                )

        # Busca o SHA do HEAD do PR quando em review+reviewed para que o
        # executor possa detectar push pós-review sem fazer I/O ele mesmo.
        # Também lê mergeability para detecção de conflito (crewflow:conflito).
        pr_head_sha: str | None = None
        pr_mergeable: str | None = None
        if result.current_state is State.REVIEW:
            import contextlib
            with contextlib.suppress(Exception):
                _repo = (
                    result.item.key.split("/issues/")[0].replace("https://github.com/", "")
                    or (repos[0] if repos else "")
                )
                _issue_number = int(result.item.key.split("/issues/")[-1]) if "/issues/" in result.item.key else 0
                if _issue_number and hasattr(provider, "get_pr_for_issue"):
                    _pr = provider.get_pr_for_issue(_repo, _issue_number)
                    if _pr:
                        pr_head_sha = _pr.get("headRefOid") or _pr.get("headRefName")
                        pr_mergeable = _pr.get("mergeable")  # "MERGEABLE" | "CONFLICTING" | "UNKNOWN"

        decision = decide(result, state_comment=state_comment, squad=squad, pr_head_sha=pr_head_sha, pr_mergeable=pr_mergeable)

        # Loga o template resolvido pelo executor e a ação decidida, para
        # cada issue processada — facilita debugar por que uma issue foi para
        # REBRAND (GATE 0) em vez de DISPATCH_DEV.
        template = resolve_template(result, squad)
        logger.info(
            "deployment: issue=%s template=%s action=%s",
            result.item.key,
            template,
            decision.action,
        )

        if decision.action is ActionKind.SKIP:
            continue
        if decision.action is ActionKind.BLOCK:
            blocked_bypass.append(result)
        elif decision.action is ActionKind.REBRAND:
            rebranded.append((result, decision))
        elif decision.action is ActionKind.NOTIFY_HUMAN:
            needs_human.append((result, decision, state_comment))
        elif decision.action is ActionKind.MARK_CONFLITO:
            mark_conflitos.append(result)
        elif decision.action is ActionKind.DISPATCH_CONFLICT_RESOLVER:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            if not repo:
                repo = repos[0] if repos else ""
            issue = _scan_result_to_issue(result)
            conflict_resolvers.append((repo, issue))
        elif decision.action is ActionKind.DISPATCH_REVIEWER:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            issue = _scan_result_to_issue(result)
            dispatch_reviewers.append((repo, issue))
        elif decision.action is ActionKind.MERGE_PR:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            issue = _scan_result_to_issue(result)
            merge_prs.append((repo, issue, state_comment))
        elif decision.action is ActionKind.DISPATCH_REWORK:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            if not repo:
                repo = repos[0] if repos else ""
            issue = _scan_result_to_issue(result)
            dispatch_reworks.append((repo, issue, state_comment))
        elif decision.action is ActionKind.DISPATCH_DEV:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            if not repo:
                repo = repos[0] if repos else ""
            issue = _scan_result_to_issue(result)
            dispatch_devs.append((repo, issue, decision))

    # ── Identifica candidatos de sessão morta (dev + running sem sinais de vida) ──
    # Verificação rápida sem I/O — a decisão final de morte usa _is_dead_session()
    # mais adiante, com timeout e verificação de sinal. Isso apenas pré-filtra para
    # que o early-return não pule a detecção de morte.
    for _r_ds in scan_results:
        if (
            _r_ds.current_state is not None
            and _r_ds.current_state.value == "crewflow:dev"
            and _Modifier.RUNNING in _r_ds.modifiers
        ):
            _repo_ds = _r_ds.item.key.split("/issues/")[0].replace("https://github.com/", "") or (repos[0] if repos else "")
            _num_str_ds = _r_ds.item.key.split("/issues/")[-1] if "/issues/" in _r_ds.item.key else "0"
            _num_ds = int(_num_str_ds) if _num_str_ds.isdigit() else 0
            if _num_ds and not _issue_has_active_session(_repo_ds, _num_ds, dev_root):
                dead_session_candidates.append(_r_ds)

    # ── Resumo do ciclo — sempre emitido, mesmo que tudo seja SKIP ──────────
    _log_cycle_summary(
        ctx=ctx,
        chat_id=chat_id,
        scan_total=len(scan_results),
        dispatch_dev=len(dispatch_devs),
        dispatch_reviewer=len(dispatch_reviewers),
        dispatch_rework=len(dispatch_reworks),
        conflict_resolver=len(conflict_resolvers),
        mark_conflito=len(mark_conflitos),
        merge_pr=len(merge_prs),
        notify_human=len(needs_human),
        block=len(blocked_bypass),
        rebrand=len(rebranded),
        spec_invalid=len(spec_invalid),
    )

    # Sem nada a fazer?
    if not any([spec_invalid, dispatch_devs, dispatch_reviewers, dispatch_reworks,
                conflict_resolvers, mark_conflitos,
                needs_human, blocked_bypass, rebranded, merge_prs, dead_session_candidates]):
        return

    # ── Modo dry-run: imprime relatório e encerra sem executar ────────────
    if dry_run:
        _dry_run_report(
            scan_results=scan_results,
            dispatch_devs=dispatch_devs,
            dispatch_reviewers=dispatch_reviewers,
            dispatch_reworks=dispatch_reworks,
            needs_human=needs_human,
            blocked_bypass=blocked_bypass,
            rebranded=rebranded,
            merge_prs=merge_prs,
            spec_invalid=spec_invalid,
            conflict_resolvers=conflict_resolvers,
            mark_conflitos=mark_conflitos,
        )
        return

    # ── Notifica specs inválidas ──────────────────────────────────────────
    if spec_invalid:
        vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
        linhas = "\n".join(
            f"  - {r.item.key}: {r.item.title}"
            for r in spec_invalid
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: {len(spec_invalid)} spec(s) sem repo declarado — "
            f"corrija o título antes de priorizar.{vm}\n{linhas}"
        )

    # ── Notifica bypass bloqueados ────────────────────────────────────────
    if blocked_bypass:
        vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
        linhas = "\n".join(f"  - {r.item.key}: {r.item.title}" for r in blocked_bypass)
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: merge bloqueado — hml-bypass sem justificativa.{vm}\n{linhas}"
        )

    # ── Aplica rebranding (troca de template) ─────────────────────────────
    for result, decision in rebranded:  # type: ignore[assignment]
        try:
            # Atualiza as labels para refletir o novo template
            current_labels = list(result.item.labels)
            for lbl in decision.remove_labels:
                if lbl in current_labels:
                    current_labels.remove(lbl)
            current_labels.extend(decision.add_labels)
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            provider.set_labels(repo, result.item.key, current_labels)
            logger.info("deployment: rebrand %s → %s", result.item.key, decision.new_template)
        except Exception as exc:
            logger.error("deployment: erro no rebrand de %s: %s", result.item.key, exc)

    # ── Dispatch do executor de dev ───────────────────────────────────────
    # Concorrência orientada ao estado da issue:
    # - cap primário: count de issues em crewflow:dev + running no scan (_running_dev_count)
    # - backstop: _active_sessions() como contagem de dispatches recentes sem label ainda
    # - anti-duplo-dispatch: _issue_has_active_session() por issue
    # - detecção de morte: _is_dead_session() para issues travadas
    _backstop_count = _active_sessions()  # dispatches recentes ainda sem label na API
    vagas = (max_conc - max(_running_dev_count, _backstop_count)) if auto else 0
    disparadas: list[tuple[str, dict]] = []
    adiadas: list[tuple[str, dict]] = []

    # Reabre o cache para leitura do running_since (detecção de sessão morta)
    _conn_disp = open_cache(scan_cfg.squad_id)

    for repo, issue, _decision in dispatch_devs:
        if not auto:
            adiadas.append((repo, issue))
            continue
        if vagas <= 0:
            adiadas.append((repo, issue))
            continue

        # Verifica se a issue já tem sessão ativa pelo estado (mecanismo primário)
        if _issue_has_active_session(repo, issue["number"], dev_root):
            logger.info(
                "deployment: sessão ativa detectada pelo estado para %s#%s — dispatch ignorado",
                repo, issue["number"],
            )
            adiadas.append((repo, issue))
            continue

        # Verifica headroom de recursos antes de cada dispatch (posture critical = skip)
        if not _resource_headroom_ok(ctx, max_conc):
            logger.warning(
                "deployment: headroom crítico — issue %s#%s adiada",
                repo, issue["number"],
            )
            adiadas.append((repo, issue))
            continue
        # Remove worktree órfão de execução anterior antes de criar o novo
        _clean_stale_worktree(dev_root, repo, issue["number"])
        try:
            prompt_extra = squad.dispatch_prompt_extra if squad else ""
            _dispatch(ctx, repo, issue, cfg, prompt_extra=prompt_extra)
            disparadas.append((repo, issue))
            vagas -= 1
        except Exception as exc:
            logger.error("deployment: erro ao despachar %s: %s", issue.get("number"), exc)
            adiadas.append((repo, issue))

    _conn_disp.close()

    # ── Detecção e recuperação de sessões mortas (issues crewflow:dev + running sem sinal) ──
    # Não despacha automaticamente — remove crewflow:running e notifica o TL
    # para que o próximo ciclo possa redespachar a partir do estado limpo.
    for result in dead_session_candidates:
        _repo_dead = result.item.key.split("/issues/")[0].replace("https://github.com/", "") or (repos[0] if repos else "")
        _num_str = result.item.key.split("/issues/")[-1] if "/issues/" in result.item.key else "0"
        _num_dead = int(_num_str) if _num_str.isdigit() else 0
        if not _num_dead:
            continue
        # Verifica timeout via running_since no cache
        _conn_dead = open_cache(scan_cfg.squad_id)
        try:
            _rs_iso = get_running_since(_conn_dead, result.item.key)
        finally:
            _conn_dead.close()
        _rs_secs: float | None = None
        if _rs_iso:
            try:
                import datetime as _dt
                _rs_secs = _dt.datetime.fromisoformat(_rs_iso).timestamp()
            except Exception:
                pass
        if _is_dead_session(_repo_dead, _num_dead, dev_root, _rs_secs):
            _conn_rec = open_cache(scan_cfg.squad_id)
            try:
                _recover_dead_session(ctx, _repo_dead, _num_dead, provider, chat_id, _conn_rec)
            finally:
                _conn_rec.close()

    # ── Notificação de resultado de dispatch ──────────────────────────────
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    if auto and disparadas:
        linhas = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in disparadas)
        extra = ""
        if adiadas:
            fila = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in adiadas)
            extra = f"\n\nNA FILA:\n{fila}"
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: disparei sessão(ões) one-shot.{vm}\n{linhas}{extra}"
        )
    elif auto and adiadas:
        fila = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in adiadas)
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: {len(adiadas)} issue(s) na fila (limite cheio).{vm}\n{fila}"
        )
    elif not auto and dispatch_devs:
        blocos = "\n".join(
            f"  - {repo}#{issue['number']}: {issue['title']}"
            for repo, issue, _ in dispatch_devs
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow (Fase 1): {len(dispatch_devs)} issue(s) prontas.{vm}\n{blocos}"
        )

    # Notificações para humanos (NOTIFY_HUMAN)
    if needs_human:
        _notify_human_actions(ctx, needs_human, chat_id)
        # Aplica mudanças de label da decisão (ex: crewflow:changes-requested)
        for result, decision, _sc in needs_human:  # type: ignore[assignment]
            _add = getattr(decision, "add_labels", ())
            _remove = getattr(decision, "remove_labels", ())
            if not _add and not _remove:
                continue
            import contextlib
            with contextlib.suppress(Exception):
                _key = result.item.key
                _repo_lbl = _key.split("/issues/")[0].replace("https://github.com/", "") or (repos[0] if repos else "")
                current_labels = list(result.item.labels)
                for lbl in _remove:
                    if lbl in current_labels:
                        current_labels.remove(lbl)
                for lbl in _add:
                    if lbl not in current_labels:
                        current_labels.append(lbl)
                provider.set_labels(_repo_lbl, _key, current_labels)
                logger.info("deployment: labels atualizadas para %s: +%s -%s", _key, list(_add), list(_remove))

    if dispatch_reviewers:
        for repo, issue in dispatch_reviewers:
            issue_number = issue["number"]
            if _reviewer_has_active(repo, issue_number):
                logger.info(
                    "deployment: reviewer já ativo para %s#%s — dispatch ignorado",
                    repo, issue_number,
                )
                continue
            try:
                _dispatch_reviewer(ctx, repo, issue, cfg)
            except Exception as exc:
                logger.error(
                    "deployment: erro ao despachar reviewer para %s#%s: %s",
                    repo, issue_number, exc,
                )

    if dispatch_reworks:
        for repo, issue, state_comment_rework in dispatch_reworks:
            issue_number = issue["number"]
            if not auto:
                vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
                ctx.notify(  # type: ignore[attr-defined]
                    f"KiroCrew Flow (Fase 1): re-trabalho pendente — "
                    f"{repo}#{issue_number}: {issue['title']}.{vm}\n"
                    f"  Reviewer pediu mudanças. Ative auto_dispatch para despachar automaticamente."
                )
                continue
            if _rework_has_active(repo, issue_number):
                logger.info(
                    "deployment: sessão de re-trabalho já ativa para %s#%s — dispatch ignorado",
                    repo, issue_number,
                )
                continue
            # Localiza PR e lê iterações atuais para o prompt
            branch = f"feat/issue-{issue_number}"
            pr_number_rework: int | None = None
            try:
                import subprocess as _sp
                _pr_res = _sp.run(
                    ["gh", "pr", "list", "--repo", repo, "--head", branch,
                     "--state", "open", "--json", "number"],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if _pr_res.returncode == 0:
                    _prs = json.loads(_pr_res.stdout or "[]")
                    if _prs:
                        pr_number_rework = int(_prs[0]["number"])
            except Exception as exc_pr:
                logger.warning(
                    "deployment: erro ao localizar PR para rework %s#%s: %s",
                    repo, issue_number, exc_pr,
                )
            if pr_number_rework is None:
                vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
                logger.warning(
                    "deployment: PR aberto não encontrado para rework %s#%s — notificando",
                    repo, issue_number,
                )
                ctx.notify(  # type: ignore[attr-defined]
                    f"KiroCrew Flow: re-trabalho pendente mas PR não localizado — "
                    f"{repo}#{issue_number}.{vm}"
                )
                continue
            from flow.audit.state_comment import get_review_iterations_from_comment
            iteration = get_review_iterations_from_comment(state_comment_rework) + 1
            try:
                prompt_extra_rework = squad.dispatch_prompt_extra if squad else ""
                _dispatch_rework(ctx, repo, issue, pr_number_rework, iteration, cfg,
                                 prompt_extra=prompt_extra_rework)
            except Exception as exc:
                logger.error(
                    "deployment: erro ao despachar rework para %s#%s: %s",
                    repo, issue_number, exc,
                )

    if merge_prs:
        _execute_auto_merges(ctx, merge_prs, chat_id, provider)

    # ── Aplica crewflow:conflito nas PRs com conflito detectado ──────────
    if mark_conflitos:
        for result in mark_conflitos:
            try:
                _repo_mc = result.item.key.split("/issues/")[0].replace("https://github.com/", "") or (repos[0] if repos else "")
                current_labels = list(result.item.labels)
                for lbl in ("crewflow:conflito",):
                    if lbl not in current_labels:
                        current_labels.append(lbl)
                provider.set_labels(_repo_mc, result.item.key, current_labels)
                logger.info("deployment: crewflow:conflito aplicado em %s", result.item.key)
            except Exception as exc:
                logger.error("deployment: erro ao aplicar conflito em %s: %s", result.item.key, exc)
        vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
        linhas_mc = "\n".join(f"  - {r.item.key}: {r.item.title}" for r in mark_conflitos)
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: {len(mark_conflitos)} PR(s) com conflito de merge detectado — "
            f"crewflow:conflito aplicado.{vm}\n{linhas_mc}"
        )

    # ── Cron de conflito: resolve rebase na branch existente ─────────────
    if conflict_resolvers:
        for repo, issue in conflict_resolvers:
            issue_number_cr = issue["number"]
            if not auto:
                vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
                ctx.notify(  # type: ignore[attr-defined]
                    f"KiroCrew Flow (Fase 1): conflito pendente — "
                    f"{repo}#{issue_number_cr}: {issue['title']}.{vm}\n"
                    f"  Ative auto_dispatch para despachar o resolvedor de conflito automaticamente."
                )
                continue
            if _conflict_resolver_has_active(repo, issue_number_cr):
                logger.info(
                    "deployment: resolvedor de conflito já ativo para %s#%s — dispatch ignorado",
                    repo, issue_number_cr,
                )
                continue
            # Localiza o PR existente para passar o número ao prompt
            branch_cr = f"feat/issue-{issue_number_cr}"
            pr_number_cr: int | None = None
            try:
                import subprocess as _sp2
                _pr_cr_res = _sp2.run(
                    ["gh", "pr", "list", "--repo", repo, "--head", branch_cr,
                     "--state", "open", "--json", "number"],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if _pr_cr_res.returncode == 0:
                    _prs_cr = json.loads(_pr_cr_res.stdout or "[]")
                    if _prs_cr:
                        pr_number_cr = int(_prs_cr[0]["number"])
            except Exception as exc_cr:
                logger.warning(
                    "deployment: erro ao localizar PR para conflict resolver %s#%s: %s",
                    repo, issue_number_cr, exc_cr,
                )
            if pr_number_cr is None:
                vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
                logger.warning(
                    "deployment: PR aberto não encontrado para conflict resolver %s#%s — notificando",
                    repo, issue_number_cr,
                )
                ctx.notify(  # type: ignore[attr-defined]
                    f"KiroCrew Flow: crewflow:conflito mas PR não localizado — "
                    f"{repo}#{issue_number_cr}.{vm}"
                )
                continue
            try:
                prompt_extra_cr = squad.dispatch_prompt_extra if squad else ""
                _dispatch_conflict_resolver(ctx, repo, issue, pr_number_cr, cfg,
                                            prompt_extra=prompt_extra_cr)
            except Exception as exc:
                logger.error(
                    "deployment: erro ao despachar conflict resolver para %s#%s: %s",
                    repo, issue_number_cr, exc,
                )


def _notify_human_actions(ctx: object, items: list, chat_id: str) -> None:
    """Notifica o humano certo sobre ações pendentes.

    Anti-spam para gate-tl: só notifica se o state_comment não tiver
    status 'awaiting-tl-approval' — evita re-notificar a cada ciclo.
    Quando notifica, atualiza o comentário com esse status via GitHub API.
    """
    from flow.audit.state_comment import parse as _parse_sc
    from flow.audit.state_comment import render as _render_sc
    from flow.executor.executor import HumanRole
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    # Separa por papel; cada item é (ScanResult, ExecutorDecision, state_comment|None)
    tl_items = [
        (r, d, sc) for r, d, sc in items
        if getattr(d, "notify_role", None) is HumanRole.TL
    ]
    dev_items = [
        (r, d, sc) for r, d, sc in items
        if getattr(d, "notify_role", None) is HumanRole.DEV
    ]
    qa_items = [
        (r, d, sc) for r, d, sc in items
        if getattr(d, "notify_role", None) is HumanRole.QA
    ]

    # TL — notificação especial com instrução de aprovação + anti-spam
    tl_to_notify = []
    for r, d, sc in tl_items:
        # Anti-spam: se já estava aguardando aprovação, não re-notifica
        sc_obj = _parse_sc(sc) if sc else None
        already_waiting = sc_obj is not None and sc_obj.status == "awaiting-tl-approval"
        if already_waiting:
            logger.info(
                "deployment: NOTIFY_HUMAN TL já notificado anteriormente, pulando: %s",
                r.item.key,
            )
            continue
        tl_to_notify.append((r, d))

        # Se o state comment tem ReviewerResult, posta no PR antes de notificar o TL
        # (caso: reviewer retornou pedidos de mudança → NOTIFY_HUMAN TL)
        if sc_obj is not None and sc_obj.reviewer_result is not None:
            import contextlib
            with contextlib.suppress(Exception):
                key = r.item.key  # https://github.com/owner/repo/issues/N
                parts = key.rstrip("/").split("/")
                if len(parts) >= 5:
                    repo_path = f"{parts[-4]}/{parts[-3]}"
                    issue_num = int(parts[-1])
                    _post_reviewer_result_on_pr(repo_path, issue_num, key, sc)

        # Atualiza o status no comentário de estado para evitar re-notificação
        if sc_obj is not None:
            from datetime import datetime
            sc_obj.status = "awaiting-tl-approval"
            import contextlib
            with contextlib.suppress(Exception):
                from flow.adapters.github_client import upsert_state_comment
                # Extrai repo e número da issue da key
                key = r.item.key  # https://github.com/owner/repo/issues/N
                parts = key.rstrip("/").split("/")
                if len(parts) >= 5:
                    repo_path = f"{parts[-4]}/{parts[-3]}"
                    issue_num_str = parts[-1]
                    updated_body = _render_sc(sc_obj)
                    upsert_state_comment(repo_path, issue_num_str, updated_body)

    if tl_to_notify:
        from datetime import datetime
        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M")
        linhas_detail = []
        for r, d in tl_to_notify:
            issue_url = r.item.key
            issue_num = issue_url.rstrip("/").split("/")[-1]
            approval_block = (
                f"<!-- KIRO-FLOW-STATE -->\n"
                f"### Aprovações\n"
                f"| Gate | Aprovado por | Quando |\n"
                f"|------|-------------|--------|\n"
                f"| `gate-tl` | @seu-usuario | {now} |\n"
                f"<!-- /KIRO-FLOW-STATE -->"
            )
            linhas_detail.append(
                f"  📋 #{issue_num}: {r.item.title}\n"
                f"     Issue: {issue_url}\n"
                f"     Motivo: {d.reason}\n\n"
                f"     Para aprovar, comente na issue:\n"
                f"     {approval_block}"
            )
        msg = (
            f"🧾 KiroCrew Flow — GATE DT aguarda aprovação do TL{vm}\n\n"
            + "\n\n".join(linhas_detail)
        )
        ctx.notify(msg)  # type: ignore[attr-defined]

    if dev_items:
        linhas = "\n".join(
            f"  - {r.item.key}: {r.item.title} — {d.reason}"
            for r, d, _sc in dev_items
        )
        ctx.notify(f"KiroCrew Flow: aguarda ação do Dev.{vm}\n{linhas}")  # type: ignore[attr-defined]
    if qa_items:
        linhas = "\n".join(
            f"  - {r.item.key}: {r.item.title} — {d.reason}"
            for r, d, _sc in qa_items
        )
        ctx.notify(f"KiroCrew Flow: aguarda ação do QA.{vm}\n{linhas}")  # type: ignore[attr-defined]


def _reviewer_has_active(repo: str, issue_number: int) -> bool:
    """Retorna True se já existe sessão one-shot do reviewer ativa para esta issue.

    Análogo ao ``_repo_has_active``, mas escopado ao slot do reviewer:
    ``reviewer-<short_repo>-<issue_number>``.
    """
    short = repo.split("/")[-1]
    locks = glob.glob(
        os.path.join(_sessdir(), f"dashboard_reviewer-{short}-{issue_number}.jsonl.lock")
    )
    return any(not _lock_is_stale(p) for p in locks)


def _reviewer_prompt(repo: str, pr_number: int, issue_number: int, head_sha: str = "") -> str:
    """Carrega e renderiza o template MD do estágio 'reviewer'.

    Usa ``flow/prompts/reviewer.md`` como fonte primária. Em caso de arquivo
    ausente, cai no fallback embutido. Variável faltando → ``PromptRenderError``.

    A fonte única de verdade para o formato dos comentários de review continua
    sendo ``flow/audit/state_comment.py`` — o template referencia os exemplares
    produzidos por esses helpers, não strings hardcoded.

    Args:
        head_sha: headRefOid atual do PR, lido pelo driving adapter antes do
                  dispatch. Injetado no prompt para que o reviewer use o SHA
                  correto no ReviewerResult sem depender de re-buscar o diff
                  do contexto do dispatch, que pode estar desatualizado.
                  String vazia ("") quando não foi possível obter o SHA.
    """
    from flow.audit.state_comment import ReviewerResult

    _rr_ok = ReviewerResult(approved=True, comments=(), sha="<sha>", reviewer="kiro-reviewer")
    _rr_ko = ReviewerResult(
        approved=False,
        comments=("<mudança 1>", "<mudança 2>"),
        sha="<sha>",
        reviewer="kiro-reviewer",
    )

    def _indent(text: str, prefix: str = "     ") -> str:
        return "\n".join(prefix + line if line else line for line in text.splitlines())

    exemplo_aprovado = _indent(render_pr_review_comment(_rr_ok, issue_number=issue_number))
    exemplo_mudancas = _indent(render_pr_review_comment(_rr_ko, issue_number=issue_number))
    short = repo.split("/")[-1]

    _reviewer_fallback = (
        "# review: {{repo_short}} PR #{{pr_number}} (issue #{{issue_number}})\n\n"
        "## Agente\n\n"
        "| Campo | Valor |\n"
        "|-------|-------|\n"
        "| Repo | `{{repo}}` |\n"
        "| PR | [#{{pr_number}}](https://github.com/{{repo}}/pull/{{pr_number}}) |\n"
        "| Issue | #{{issue_number}} |\n"
        "| HEAD SHA (dispatch) | `{{head_sha}}` |\n\n"
        "## Contexto da task\n\n"
        "Você é um agente de code review ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.\n\n"
        "### Fluxo\n\n"
        "Execute UMA vez, do início ao fim, e PARE:\n\n"
        "1. Leia a issue para ter contexto, incluindo os comentários:\n"
        "   gh issue view {{issue_number}} --repo {{repo}}\n"
        "   gh issue view {{issue_number}} --repo {{repo}} --comments\n"
        "2. Leia o diff do PR ancorado no HEAD atual e os comentários do PR:\n"
        "   IMPORTANTE: o SHA do HEAD no momento do dispatch está fixado acima em \"HEAD SHA\".\n"
        "   Registre-o como o SHA desta revisão no ReviewerResult (passo 9). NÃO use SHA de contexto anterior.\n"
        "   gh pr diff {{pr_number}} --repo {{repo}}\n"
        "   gh pr view {{pr_number}} --repo {{repo}} --comments\n"
        "   Confirme que o headRefOid atual bate com {{head_sha}}:\n"
        "   gh pr view {{pr_number}} --repo {{repo}} --json headRefOid\n"
        "   Se o SHA retornado for diferente de {{head_sha}}, use o SHA retornado (a PR pode ter avançado)\n"
        "   e anote no ReviewerResult.\n"
        "3. Leia os steerings do repo (.kiro/steering/*.md) para entender convenções.\n"
        "4. Verifique o status da pipeline de CI do PR:\n"
        "   gh pr checks {{pr_number}} --repo {{repo}} --json name,state,conclusion\n"
        "   O CI deve estar VERDE (todos os checks com conclusion=success ou state=success).\n"
        "   Se algum check estiver em pending/in_progress: aguarde e verifique novamente antes de concluir.\n"
        "   CI com failure/error = bloqueio para aprovação (mesmo que o código esteja correto).\n"
        "5. Analise: corretude, cobertura de testes, estilo, convenções do projeto.\n"
        "   Considere também comentários não resolvidos do PR (passo 2) — comentários abertos\n"
        "   de revisores humanos devem ser tratados como pedidos de mudança pendentes.\n"
        "6. DECIDA: o PR está aprovado SE E SOMENTE SE:\n"
        "   - CI verde (todos os checks passaram, passo 4)\n"
        "   - Nenhum comentário de mudança no PR (revisores humanos ou automated) não resolvido (passo 2)\n"
        "   - Análise técnica sem blockers (passo 5)\n"
        "   Se qualquer uma das três condições falhar → pedidos de mudança (não aprova).\n"
        "7. POSTE O RESULTADO COMPLETO DO REVIEW NO PR:\n"
        "   Use exatamente este formato no comentário do PR:\n"
        "   Se APROVADO sem comentários (omita a seção `### Pedidos de mudança`):\n"
        "{{example_approved}}\n"
        "   Se houver pedidos de mudança:\n"
        "{{example_changes}}\n"
        "8. POSTE O RESULTADO COMPLETO DO REVIEW NA ISSUE #{{issue_number}} também:\n"
        "   - Cole o mesmo comentário completo (mesmo corpo do passo 7) na issue:\n"
        "     gh issue comment {{issue_number}} --repo {{repo}} --body \"<mesmo corpo completo>\"\n"
        "   O resultado COMPLETO deve aparecer nos DOIS lugares — PR e issue.\n"
        "   NÃO poste só uma referência curta: o resultado completo vai nos dois.\n"
        "9. Registre o resultado no state_comment DA ISSUE com ReviewerResult:\n"
        "   - Se APROVADO (CI verde + zero comentários + sem blockers): `approved: true`, `comments: []`\n"
        "   - Se tem pedidos de mudança: `approved: false`, `comments: [\"<mudança 1>\", ...]`\n"
        "   - Inclua o motivo de CI vermelho como primeiro item em `comments` se aplicável\n"
        "   Use `upsert_state_comment` para atualizar o bloco <!-- KIRO-FLOW-STATE --> NA ISSUE.\n"
        "   O ReviewerResult DEVE incluir o headRefOid lido no passo 2 como campo `sha`.\n"
        "   Use o SHA obtido via `gh pr view {{pr_number}} --repo {{repo}} --json headRefOid` no passo 2\n"
        "   — não {{head_sha}} hardcoded, pois a PR pode ter avançado entre o dispatch e a execução.\n"
        "   IMPORTANTE: o ReviewerResult PERMANECE na issue — é o que o scan lê pra decidir MERGE_PR.\n"
        "10. Se aprovado (zero comentários + CI verde): troque as labels da issue:\n"
        "    `gh issue edit {{issue_number}} --repo {{repo}} --add-label \"crewflow:review-ok\" --remove-label \"crewflow:review,crewflow:reviewed\"`\n"
        "11. Se tem comentários ou CI vermelho: troque as labels da issue:\n"
        "    `gh issue edit {{issue_number}} --repo {{repo}} --add-label \"crewflow:review-fail\" --remove-label \"crewflow:review,crewflow:reviewed\"`\n"
        "12. ENCERRE.\n\n"
        "### Regras críticas\n\n"
        "- UMA passada. Terminou, acabou. NÃO entre em loop.\n"
        "- NUNCA mergeie. NUNCA faça deploy.\n"
        "- Seja objetivo — aponte problemas concretos, não estilo pessoal.\n"
        "- CI vermelho sempre bloqueia — mesmo que o código pareça correto.\n"
        "- Resultado completo vai em DOIS lugares: PR (passo 7) e issue (passo 8).\n"
    )

    try:
        return render_prompt(
            "reviewer",
            fallback=_reviewer_fallback,
            repo=repo,
            repo_short=short,
            pr_number=str(pr_number),
            issue_number=str(issue_number),
            head_sha=head_sha,
            example_approved=exemplo_aprovado,
            example_changes=exemplo_mudancas,
        )
    except PromptRenderError:
        logger.exception(
            "deployment: erro ao renderizar template 'reviewer' para %s PR#%s — dispatch abortado",
            repo, pr_number,
        )
        raise


_REWORK_PROMPT_FALLBACK = (
    "# {{session_title}}\n\n"
    "## Agente\n\n"
    "| Campo | Valor |\n"
    "|-------|-------|\n"
    "| Repo | `{{repo}}` |\n"
    "| Issue | [#{{issue_number}}]({{issue_url}}) — {{issue_title}} |\n"
    "| PR | #{{pr_number}} |\n\n"
    "## Contexto da task\n\n"
    "Você é um agente de RE-TRABALHO pós-review ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.\n"
    "Seu único objetivo: aplicar os pedidos de mudança do reviewer na PR existente e devolver a issue para review.\n\n"
    "### Fluxo\n\n"
    "Execute UMA vez, do início ao fim, e PARE:\n\n"
    "1. CONTEXTO — leia tudo antes de agir:\n"
    "   - `.kiro/steering/*.md` (steerings do projeto)\n"
    "   - A issue e seus comentários:\n"
    "     `gh issue view {{issue_number}} --repo {{repo}}`\n"
    "     `gh issue view {{issue_number}} --repo {{repo}} --comments`\n"
    "   - O diff da PR e os comentários do reviewer:\n"
    "     `gh pr diff {{pr_number}} --repo {{repo}}`\n"
    "     `gh pr view {{pr_number}} --repo {{repo}} --comments`\n"
    "   Os comentários do reviewer NA PR são a FONTE DA VERDADE dos pedidos de mudança.\n"
    "   Leia-os todos antes de escrever qualquer código.\n"
    "2. ESCOPO: aplique APENAS os pedidos de mudança listados pelo reviewer.\n"
    "   - NÃO adicione features extras.\n"
    "   - NÃO refatore código não mencionado.\n"
    "   - Se um pedido for ambíguo, comente na PR pedindo esclarecimento, marque `crewflow:blocked` e ENCERRE.\n"
    "3. USE O WORKTREE E BRANCH EXISTENTES — NÃO crie branch nova, NÃO abra PR novo.\n"
    "   A branch feat/issue-{{issue_number}} já existe. Use-a:\n"
    "   `cd {{worktree_path}}`\n"
    "   Se o worktree não existir (foi removido após a PR), re-crie-o:\n"
    "   `cd {{dev_root}}/{{repo_short}} && git fetch origin && git worktree add {{worktree_path}} feat/issue-{{issue_number}}`\n"
    "   Trabalhe DENTRO do worktree; NUNCA toque em outros worktrees.\n"
    "4. Implemente as correções solicitadas pelo reviewer.\n"
    "5. **VALIDAÇÃO OBRIGATÓRIA — rode ANTES de fazer push.** Se o repo for `eliasrosa/kirocrew-flow`, execute exatamente:\n"
    "   ```bash\n"
    "   python3 -m ruff check flow/\n"
    "   python3 -m mypy flow/ --ignore-missing-imports\n"
    "   python3 -m pytest flow/tests/ --cov=flow --cov-fail-under=75\n"
    "   ```\n"
    "   Para outros repos, descubra os comandos via README/Makefile/pyproject — **não presuma**.\n"
    "   Se qualquer check falhar e você não conseguir corrigir, marque `crewflow:blocked` e ENCERRE. "
    "**Não faça push com CI vermelho.**\n"
    "6. Faça commit e push na branch existente:\n"
    "   `git add -A && git commit -m \"fix: aplicar pedidos de mudança do reviewer (iteração {{iteration}})\" && git push origin feat/issue-{{issue_number}}`\n"
    "   Isso remove automaticamente `crewflow:reviewed` (novo SHA invalida o lock anti-loop).\n"
    "7. Atualize o state_comment da issue incrementando `review_iterations`:\n"
    "   - Leia o comentário atual: `gh issue view {{issue_number}} --repo {{repo}} --comments`\n"
    "   - Incremente o campo `**Iterações de review:**` (ou adicione-o se ausente)\n"
    "   - Adicione uma linha no histórico: `| <data> | rework → review | kiro-dev |`\n"
    "   - Atualize via `gh issue comment {{issue_number}} --repo {{repo}} --body \"...\"` (editando o comentário existente)\n"
    "8. Troque a label de volta para review:\n"
    "   `gh issue edit {{issue_number}} --repo {{repo}} --remove-label \"crewflow:running,crewflow:review-fail\" --add-label \"crewflow:review\"`\n"
    "9. Ao terminar: {{notify_step}}\n\n"
    "   remova `crewflow:running`, mantenha `crewflow:review`, e ENCERRE.\n\n"
    "{{vault_step}}\n\n"
    "### Regras críticas\n\n"
    "- UMA passada. Terminou, acabou. NÃO entre em loop.\n"
    "- NUNCA mergeie. NUNCA faça deploy.\n"
    "- NUNCA abra PR novo — use a branch feat/issue-{{issue_number}} existente.\n"
    "- Aplique APENAS os pedidos explícitos do reviewer. Nada além.\n"
    "- Se bloquear, marque `crewflow:blocked`, avise, e pare.\n\n"
    "{{prompt_extra}}"
)


def _rework_prompt(
    repo: str,
    issue: dict,
    pr_number: int,
    iteration: int,
    cfg: dict,
    prompt_extra: str = "",
) -> str:
    """Carrega e renderiza o template MD do estágio 'rework'.

    Usa ``flow/prompts/rework.md`` como fonte primária. Em caso de arquivo ausente
    ou corrompido, cai no fallback embutido ``_REWORK_PROMPT_FALLBACK``.
    Variável faltando → ``PromptRenderError`` (fail-closed).
    """
    short = repo.split("/")[-1]
    vault = cfg.get("vault_root") or ""
    dev_root = cfg.get("dev_root") or os.path.expanduser("~/dev")
    chat_id = cfg.get("notify_chat_id") or ""

    vault_step = ""
    if vault:
        vault_step = (
            f"   - VAULT: edite `{vault}/Projetos/{short}/backlog.md` refletindo o progresso "
            f"e sincronize com `sh {vault}/.sync.sh \"<msg>\"` (NUNCA `git push` literal). "
            "Se a pasta não existir, pule sem erro."
        )
    notify_step = (
        f"avise via voice_maybe (chat_id {chat_id}, intent auto) com TL;DR, "
        if chat_id else "reporte o resultado, "
    )
    worktree = _worktree_path(dev_root, repo, issue["number"])
    session_title = f"rework: {short} #{issue['number']} PR #{pr_number} (iter {iteration}): {issue['title']}"

    try:
        return render_prompt(
            "rework",
            fallback=_REWORK_PROMPT_FALLBACK,
            repo=repo,
            repo_short=short,
            issue_number=str(issue["number"]),
            issue_title=issue["title"],
            issue_url=issue.get("url") or f"https://github.com/{repo}/issues/{issue['number']}",
            session_title=session_title,
            pr_number=str(pr_number),
            dev_root=dev_root,
            worktree_path=worktree,
            iteration=str(iteration),
            notify_step=notify_step,
            vault_step=vault_step,
            prompt_extra=prompt_extra.strip(),
        )
    except PromptRenderError:
        logger.exception(
            "deployment: erro ao renderizar template 'rework' para %s#%s — dispatch abortado",
            repo, issue["number"],
        )
        raise


def _rework_has_active(repo: str, issue_number: int) -> bool:
    """Retorna True se já existe sessão de re-trabalho ativa para esta issue."""
    short = repo.split("/")[-1]
    # Slot do re-trabalho usa o mesmo padrão da sessão dev inicial
    locks = glob.glob(
        os.path.join(_sessdir(), f"dashboard_esteira-{short}-{issue_number}.jsonl.lock")
    )
    return any(not _lock_is_stale(p) for p in locks)


def _dispatch_rework(
    ctx: object,
    repo: str,
    issue: dict,
    pr_number: int,
    iteration: int,
    cfg: dict,
    prompt_extra: str = "",
) -> None:
    """Fire-and-forget POST /api/chat para a sessão one-shot de re-trabalho.

    Reusa o mesmo slot da sessão dev para garantir one-per-repo funcione.
    """
    # Guard: se a issue já está fechada, abortar (guard #136).
    if _is_issue_closed(repo, issue["number"]):
        logger.info(
            "deployment: issue %s#%s está CLOSED — dispatch de rework ignorado (guard #136)",
            repo, issue["number"],
        )
        return

    import urllib.request as _u

    short = repo.split("/")[-1]
    slot = f"esteira-{short}-{issue['number']}"

    try:
        message = _rework_prompt(repo, issue, pr_number, iteration, cfg, prompt_extra=prompt_extra)
    except PromptRenderError as exc:
        logger.error(
            "deployment: _dispatch_rework abortado — template 'rework' inválido para %s#%s: %s",
            repo, issue["number"], exc,
        )
        return

    body = json.dumps({
        "message": message,
        "agent": cfg.get("agent") or "kirocrew",
        "slot": slot,
        "memory_mode": "temporary",
    }).encode()
    req = _u.Request(
        f"http://localhost:{ctx._port}/api/chat",  # type: ignore[attr-defined]
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": ctx._secret,  # type: ignore[attr-defined]
            "X-Session-Key": f"cron:{ctx.job.id}",  # type: ignore[attr-defined]
        },
        method="POST",
    )
    try:
        from kiro_crew.loopback_http import loopback_urlopen  # type: ignore[import]
        with loopback_urlopen(req, timeout=3) as resp:
            resp.read(1)
        logger.info(
            "deployment: rework one-shot despachado para %s#%s (PR #%s, iter %s)",
            repo, issue["number"], pr_number, iteration,
        )
    except Exception as exc:
        logger.error(
            "deployment: falha ao despachar rework para %s#%s: %s",
            repo, issue["number"], exc,
        )


def _conflict_resolver_has_active(repo: str, issue_number: int) -> bool:
    """Retorna True se já existe sessão de resolução de conflito ativa para esta issue."""
    short = repo.split("/")[-1]
    locks = glob.glob(
        os.path.join(_sessdir(), f"dashboard_esteira-{short}-{issue_number}.jsonl.lock")
    )
    return any(not _lock_is_stale(p) for p in locks)


# Fallback embutido para o prompt do conflict resolver — usado quando
# flow/prompts/conflict.md não existe no disco.
_CONFLICT_PROMPT_FALLBACK = (
    "# {{session_title}}\n\n"
    "## Agente\n\n"
    "| Campo | Valor |\n"
    "|-------|-------|\n"
    "| Repo | `{{repo}}` |\n"
    "| Issue | [#{{issue_number}}]({{issue_url}}) — {{issue_title}} |\n"
    "| PR | #{{pr_number}} |\n\n"
    "## Contexto da task\n\n"
    "Você é um agente de RESOLUÇÃO DE CONFLITO ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.\n\n"
    "USE O WORKTREE E BRANCH EXISTENTES — NÃO crie branch nova, NÃO abra PR novo.\n"
    "  `cd {{worktree_path}}`\n"
    "  Se não existir: `cd {{dev_root}}/{{repo_short}} && git fetch origin && "
    "git worktree add {{worktree_path}} feat/issue-{{issue_number}}`\n\n"
    "### Fluxo\n\n"
    "Execute UMA vez, do início ao fim, e PARE:\n\n"
    "RESOLVA O CONFLITO:\n"
    "  `git fetch origin && git rebase origin/{{base_branch}}`\n"
    "  Resolva conflitos manualmente se necessário, depois:\n"
    "  `git push origin feat/issue-{{issue_number}} --force-with-lease`\n\n"
    "Remove `crewflow:conflito` e `crewflow:running` da issue.\n\n"
    "### Regras críticas\n\n"
    "NUNCA mergeie. NUNCA faça deploy. NUNCA abra PR novo.\n\n"
    "{{prompt_extra}}"
)


def _conflict_prompt(
    repo: str,
    issue: dict,
    pr_number: int,
    cfg: dict,
    prompt_extra: str = "",
) -> str:
    """Carrega e renderiza o template MD do estágio 'conflict'."""
    import subprocess as _sp3

    short = repo.split("/")[-1]
    dev_root = cfg.get("dev_root") or os.path.expanduser("~/dev")
    chat_id = cfg.get("notify_chat_id") or ""
    vault = cfg.get("vault_root") or ""

    # Descobre a branch base do repo
    base_branch = "main"
    try:
        _bb = _sp3.run(
            ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if _bb.returncode == 0:
            base_branch = _bb.stdout.strip() or "main"
    except Exception:
        pass

    vault_step = ""
    if vault:
        vault_step = (
            f"   - VAULT: `sh {vault}/.sync.sh \"<msg>\"` se a branch foi atualizada "
            "significativamente."
        )
    notify_step = (
        f"avise via voice_maybe (chat_id {chat_id}, intent auto) com TL;DR, "
        if chat_id else "reporte o resultado, "
    )
    worktree = _worktree_path(dev_root, repo, issue["number"])
    session_title = f"conflito: {short} #{issue['number']} PR #{pr_number}: {issue['title']}"

    try:
        return render_prompt(
            "conflict",
            fallback=_CONFLICT_PROMPT_FALLBACK,
            repo=repo,
            repo_short=short,
            issue_number=str(issue["number"]),
            issue_title=issue["title"],
            issue_url=issue.get("url", ""),
            session_title=session_title,
            pr_number=str(pr_number),
            dev_root=dev_root,
            worktree_path=worktree,
            base_branch=base_branch,
            notify_step=notify_step,
            vault_step=vault_step,
            prompt_extra=prompt_extra.strip(),
        )
    except PromptRenderError:
        logger.exception(
            "deployment: erro ao renderizar template 'conflict' para %s#%s — dispatch abortado",
            repo, issue["number"],
        )
        raise


def _dispatch_conflict_resolver(
    ctx: object,
    repo: str,
    issue: dict,
    pr_number: int,
    cfg: dict,
    prompt_extra: str = "",
) -> None:
    """Fire-and-forget POST /api/chat para a sessão one-shot de resolução de conflito."""
    # Guard: se a issue já está fechada, abortar (guard #136).
    if _is_issue_closed(repo, issue["number"]):
        logger.info(
            "deployment: issue %s#%s está CLOSED — dispatch de conflict resolver ignorado (guard #136)",
            repo, issue["number"],
        )
        return

    import urllib.request as _u

    short = repo.split("/")[-1]
    slot = f"esteira-{short}-{issue['number']}"

    try:
        message = _conflict_prompt(repo, issue, pr_number, cfg, prompt_extra=prompt_extra)
    except PromptRenderError as exc:
        logger.error(
            "deployment: _dispatch_conflict_resolver abortado — template 'conflict' inválido para %s#%s: %s",
            repo, issue["number"], exc,
        )
        return

    body = json.dumps({
        "message": message,
        "agent": cfg.get("agent") or "kirocrew",
        "slot": slot,
        "memory_mode": "temporary",
    }).encode()
    req = _u.Request(
        f"http://localhost:{ctx._port}/api/chat",  # type: ignore[attr-defined]
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": ctx._secret,  # type: ignore[attr-defined]
            "X-Session-Key": f"cron:{ctx.job.id}",  # type: ignore[attr-defined]
        },
        method="POST",
    )
    try:
        from kiro_crew.loopback_http import loopback_urlopen  # type: ignore[import]
        with loopback_urlopen(req, timeout=3) as resp:
            resp.read(1)
        logger.info(
            "deployment: conflict resolver despachado para %s#%s (PR #%s)",
            repo, issue["number"], pr_number,
        )
    except Exception as exc:
        logger.error(
            "deployment: falha ao despachar conflict resolver para %s#%s: %s",
            repo, issue["number"], exc,
        )


def _is_issue_closed(repo: str, issue_number: int) -> bool:
    """Verifica se a issue está fechada (state != 'OPEN') via gh CLI.

    Guard defensivo: se a issue for CLOSED (PR canônica mergeada), nenhum
    dispatch deve acontecer — a sessão deve ser no-op. Fail-open em caso de
    erro de I/O (retorna False → despacha normalmente).
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--repo", repo,
             "--json", "state", "--jq", ".state"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            state = result.stdout.strip().upper()
            return state == "CLOSED"
    except Exception as exc:
        logger.warning(
            "deployment: não foi possível verificar estado de %s#%s: %s — fail-open",
            repo, issue_number, exc,
        )
    return False


def _dispatch_reviewer(
    ctx: object,
    repo: str,
    issue: dict,
    cfg: dict,
) -> None:
    """Fire-and-forget POST /api/chat para a sessão one-shot do kiro-reviewer.

    Localiza o PR aberto da issue e despacha o reviewer com o prompt correto.
    Fallback para notificação se o PR não for encontrado.
    """
    issue_number = issue["number"]

    # Guard: se a issue já está fechada (PR canônica mergeada), abortar.
    # Evita a race onde o reviewer é despachado após o merge da canônica — foi
    # o que causou a PR duplicada #135 relatada na issue #136.
    if _is_issue_closed(repo, issue_number):
        logger.info(
            "deployment: issue %s#%s está CLOSED — dispatch do reviewer ignorado (guard #136)",
            repo, issue_number,
        )
        return
    chat_id = cfg.get("notify_chat_id") or ""
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    # Localiza o PR aberto para a issue
    branch = f"feat/issue-{issue_number}"
    pr_number: int | None = None
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--head", branch,
             "--state", "open", "--json", "number"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode == 0:
            prs = json.loads(result.stdout or "[]")
            if prs:
                pr_number = int(prs[0]["number"])
    except Exception as exc:
        logger.warning(
            "deployment: erro ao localizar PR para %s#%s no dispatch do reviewer: %s",
            repo, issue_number, exc,
        )

    if pr_number is None:
        # Fallback: não encontrou PR — apenas notifica
        logger.warning(
            "deployment: PR aberto não encontrado para %s#%s — notificando sem dispatch",
            repo, issue_number,
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: reviewer pendente — PR de {repo}#{issue_number} não localizado.{vm}"
        )
        return

    short = repo.split("/")[-1]
    slot = f"reviewer-{short}-{issue_number}"

    # Busca o headRefOid atual do PR antes de gerar o prompt.
    # Injetado em {{head_sha}} para que o reviewer saiba exatamente qual SHA
    # revisar — evita que o modelo use o diff do contexto do dispatch (SHA antigo).
    head_sha = ""
    try:
        _sha_res = subprocess.run(
            ["gh", "pr", "view", str(pr_number), "--repo", repo,
             "--json", "headRefOid", "--jq", ".headRefOid"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if _sha_res.returncode == 0:
            head_sha = _sha_res.stdout.strip()
    except Exception as exc_sha:
        logger.warning(
            "deployment: não foi possível obter headRefOid para %s PR#%s: %s — seguindo sem SHA",
            repo, pr_number, exc_sha,
        )

    import urllib.request as _u
    body = json.dumps({
        "message": _reviewer_prompt(repo, pr_number, issue_number, head_sha=head_sha),
        "agent": cfg.get("agent") or "kirocrew",
        "slot": slot,
        "memory_mode": "temporary",
    }).encode()
    req = _u.Request(
        f"http://localhost:{ctx._port}/api/chat",  # type: ignore[attr-defined]
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": ctx._secret,  # type: ignore[attr-defined]
            "X-Session-Key": f"cron:{ctx.job.id}",  # type: ignore[attr-defined]
        },
        method="POST",
    )
    try:
        from kiro_crew.loopback_http import loopback_urlopen  # type: ignore[import]
        with loopback_urlopen(req, timeout=3) as resp:
            resp.read(1)
        logger.info(
            "deployment: reviewer one-shot despachado para %s PR #%s (issue #%s)",
            repo, pr_number, issue_number,
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: sessão one-shot do reviewer despachada — "
            f"{repo} PR #{pr_number} (issue #{issue_number}).{vm}"
        )
    except Exception as exc:
        logger.error(
            "deployment: falha ao despachar reviewer para %s#%s: %s",
            repo, issue_number, exc,
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: falha ao despachar reviewer para {repo}#{issue_number}.{vm}"
        )


def _post_reviewer_result_on_pr(
    repo: str,
    issue_number: int,
    issue_url: str,
    state_comment: str | None,
) -> None:
    """Posta (ou atualiza) o resultado do reviewer como comentário no PR.

    Localiza o PR aberto associado à issue, depois posta o comentário formatado.
    Silencioso em caso de erro — o resultado já está na issue; o PR é bonus.
    """
    import contextlib

    from flow.adapters import github_client as gh_client
    from flow.audit.state_comment import get_reviewer_result_from_comment, render_pr_review_comment

    reviewer_result = get_reviewer_result_from_comment(state_comment)
    if reviewer_result is None:
        logger.warning(
            "deployment: _post_reviewer_result_on_pr: ReviewerResult ausente para %s#%s",
            repo, issue_number,
        )
        return

    pr = gh_client.get_pr_for_issue(repo, issue_number)
    if pr is None:
        logger.info(
            "deployment: _post_reviewer_result_on_pr: PR aberto não encontrado para %s#%s — pulando",
            repo, issue_number,
        )
        return

    pr_number = pr["number"]
    body = render_pr_review_comment(
        reviewer_result,
        issue_number=issue_number,
        issue_url=issue_url,
    )

    with contextlib.suppress(Exception):
        gh_client.upsert_pr_review_comment(repo, pr_number, body)
        logger.info(
            "deployment: resultado do reviewer postado no PR #%s (%s#%s)",
            pr_number, repo, issue_number,
        )

    # Referência curta na issue: atualiza o state comment com link para o PR
    _update_issue_with_pr_ref(repo, issue_number, pr_number, reviewer_result)


def _update_issue_with_pr_ref(
    repo: str,
    issue_number: int,
    pr_number: int,
    reviewer_result: object,
) -> None:
    """Adiciona referência curta ao PR no comentário de estado da issue.

    Adiciona linha no histórico indicando onde o review foi postado.
    Não altera o ReviewerResult — o scan continua lendo dali.
    """
    import contextlib

    from flow.adapters import github_client as gh_client
    from flow.audit.state_comment import ReviewerResult
    from flow.audit.state_comment import parse as _parse
    from flow.audit.state_comment import render as _render

    rr: ReviewerResult = reviewer_result  # type: ignore[assignment]
    state_comment_body = gh_client.get_state_comment(repo, str(issue_number))
    if state_comment_body is None:
        return

    sc = _parse(state_comment_body)
    if sc is None:
        return

    status_str = "aprovado" if rr.approved else "pedidos de mudança"
    pr_url = f"https://github.com/{repo}/pull/{pr_number}"
    sc.add_transition(
        from_state="review",
        to_state=f"review (resultado no [PR #{pr_number}]({pr_url}))",
        actor="kiro-reviewer",
    )
    sc.status = f"review postado em PR #{pr_number} — {status_str}"

    with contextlib.suppress(Exception):
        gh_client.upsert_state_comment(repo, str(issue_number), _render(sc))


def _notify_reviewers(ctx: object, items: list, chat_id: str) -> None:
    """Notifica que o kiro-reviewer foi disparado (legado — mantido como referência)."""
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
    linhas = "\n".join(f"  - {repo}#{issue['number']}: {issue['title']}" for repo, issue in items)
    ctx.notify(  # type: ignore[attr-defined]
        f"KiroCrew Flow: análise automatizada de code review disparada.{vm}\n{linhas}"
    )


def _execute_auto_merges(
    ctx: object,
    items: list,
    chat_id: str,
    provider: object,
) -> None:
    """Executa merge squash automático para PRs aprovados sem comentários.

    Para cada (repo, issue, state_comment) em ``items``:
    1. Posta resultado do reviewer no PR (e referência curta na issue)
    2. Localiza o PR aberto associado à issue
    3. Faz o merge squash via GitHub API
    4. Atualiza labels: adiciona crewflow:done, remove crewflow:review-ok (e review/reviewed se presentes)
    5. Notifica TL com resultado (sucesso ou falha)
    """
    from flow.adapters import github_client as gh_client
    from flow.ports.issue_provider import ProviderError

    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    merged: list[tuple[str, dict]] = []
    failed: list[tuple[str, dict, str]] = []

    for repo, issue, state_comment in items:
        issue_number = issue["number"]
        issue_url = issue.get("url") or f"https://github.com/{repo}/issues/{issue_number}"
        try:
            # Posta resultado do reviewer no PR antes do merge
            _post_reviewer_result_on_pr(repo, issue_number, issue_url, state_comment)

            # Localiza o PR aberto associado à issue
            pr = gh_client.get_pr_for_issue(repo, issue_number)
            if pr is None:
                failed.append((repo, issue, f"PR aberto para #{issue_number} não encontrado"))
                continue

            pr_number = pr["number"]
            pr_branch = pr.get("headRefName") or pr.get("head", {}).get("ref", "")

            # Merge squash
            gh_client.merge_pull_request(repo, pr_number, merge_method="squash")

            # Deleta a branch após confirmar o merge
            if pr_branch:
                import contextlib
                with contextlib.suppress(Exception):
                    gh_client.delete_branch(repo, pr_branch)
                    logger.info(
                        "deployment: branch %s deletada após merge de #%s",
                        pr_branch, pr_number,
                    )

            # Atualiza labels da issue: transição atômica → crewflow:done
            # Remove todos os estados anteriores (review, etc.) + modificadores usados
            try:
                item_data = gh_client.get_work_item(repo, str(issue_number))
                new_labels = _apply_state_transition(
                    item_data.get("labels", []),
                    State.DONE,
                    remove_modifiers=("crewflow:review-ok", "crewflow:reviewed", "crewflow:running"),
                )
                gh_client.set_labels(repo, str(issue_number), new_labels)
            except Exception as exc:
                logger.warning(
                    "deployment: merge ok mas falha ao atualizar labels de %s#%s: %s",
                    repo, issue_number, exc,
                )

            # Comenta na issue registrando o merge automático
            from datetime import datetime
            now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
            import contextlib
            with contextlib.suppress(Exception):
                gh_client.add_issue_comment(
                    repo,
                    issue_number,
                    f"✅ **Merge automático** — PR #{pr_number} mergeado em {now}.\n\n"
                    f"Reviewer aprovou sem comentários. Branch `{pr_branch}` deletada.",
                )

            merged.append((repo, issue))
            logger.info(
                "deployment: merge squash automático PR #%s (issue #%s) em %s",
                pr_number, issue_number, repo,
            )

        except ProviderError as exc:
            msg = str(exc)
            failed.append((repo, issue, msg))
            logger.error(
                "deployment: falha no merge automático de %s#%s: %s",
                repo, issue_number, exc,
            )
        except Exception as exc:
            msg = str(exc)
            failed.append((repo, issue, msg))
            logger.error(
                "deployment: erro inesperado no merge de %s#%s: %s",
                repo, issue_number, exc,
            )

    if merged:
        linhas = "\n".join(
            f"  ✅ {r}#{i['number']}: {i['title']}"
            for r, i in merged
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: PR(s) mergeado(s) automaticamente — reviewer sem comentários.{vm}\n{linhas}"
        )

    if failed:
        linhas = "\n".join(
            f"  ❌ {r}#{i['number']}: {i['title']} — {motivo}"
            for r, i, motivo in failed
        )
        ctx.notify(  # type: ignore[attr-defined]
            f"KiroCrew Flow: falha no merge automático.{vm}\n{linhas}"
        )


# ── Entrypoints por estágio ───────────────────────────────────────────────
#
# Cada função de entrypoint é um cron de script independente.
# Use `run_dev`, `run_reviewer`, `run_merge` e `run_conflito` em vez de `run`
# para ter crons com logs, intervalos e modelos isolados por estágio.
#
# O parâmetro `model` sobrescreve `cfg.agent` apenas para o dispatch deste
# estágio — o scanner não usa LLM (zero token), só o dispatch usa.
# Configure via `stage_models` na deployment.config.yaml:
#
#   stage_models:
#     dev:       "kirocrew"            # modelo mais forte (implementação)
#     reviewer:  "kirocrew"            # modelo mais rápido (review)
#     merge:     "kirocrew"            # merge squash (leve)
#     conflito:  "kirocrew"            # re-trabalho pós-review

_STAGE_DEV      = "dev"
_STAGE_REVIEWER = "reviewer"
_STAGE_MERGE    = "merge"
_STAGE_CONFLITO = "conflito"

# ActionKinds por estágio — o filtro que cada entrypoint aplica sobre o scan
#
# Distribuição das ações de conflito:
#   - mark_conflito      → _STAGE_REVIEWER: detectado durante o scan de review quando
#                          pr_mergeable == "CONFLICTING"; o cron reviewer já lê esse campo.
#   - dispatch_conflict_resolver → _STAGE_CONFLITO: despachado quando crewflow:conflito
#                          já foi aplicado na issue (Modifier.CONFLITO presente).
#   - dispatch_rework    → _STAGE_CONFLITO: re-trabalho pós-review (review-fail).
_STAGE_ACTIONS = {
    _STAGE_DEV:      frozenset({"dispatch_dev"}),
    _STAGE_REVIEWER: frozenset({"dispatch_reviewer", "mark_conflito"}),
    _STAGE_MERGE:    frozenset({"merge_pr"}),
    _STAGE_CONFLITO: frozenset({"dispatch_rework", "dispatch_conflict_resolver"}),
}


def _stage_model(cfg: dict, stage: str) -> str | None:
    """Retorna o agente/modelo configurado para o estágio, ou None (usa cfg["agent"])."""
    stage_models = cfg.get("stage_models") or {}
    return stage_models.get(stage) or None


def _run_stage(ctx: object, stage: str) -> None:
    """Executa o ciclo do cron restrito ao estágio indicado.

    O scan é completo (todos os estados), mas só as decisões do estágio
    são executadas. Labels, notificações e dry-run funcionam normalmente.

    Args:
        ctx:   contexto do cron do Kiro Crew
        stage: um dos valores _STAGE_* (dev/reviewer/merge/conflito)
    """
    _check_installed_version(ctx)
    cfg = _load_config()

    # Substituição de agente por modelo do estágio
    stage_agent = _stage_model(cfg, stage)
    if stage_agent:
        cfg = dict(cfg)  # cópia rasa — não muta a config global
        cfg["agent"] = stage_agent

    repos: list[str] = cfg.get("repos") or []
    auto = bool(cfg.get("auto_dispatch", False))
    max_conc = int(cfg.get("max_concurrent_tasks") or cfg.get("max_concurrent", 2))
    dev_root: str = cfg.get("dev_root") or os.path.expanduser("~/dev")
    chat_id = cfg.get("notify_chat_id") or ""
    issue_provider_name: str = cfg.get("issue_provider", "github")

    dry_run = bool(os.environ.get("CREWFLOW_DRY_RUN")) or bool(cfg.get("dry_run", False))
    if dry_run:
        logger.info("deployment[%s]: modo dry-run ativado — nenhuma ação será executada", stage)

    if not repos:
        logger.warning("deployment[%s]: nenhum repo/projeto configurado", stage)
        return

    # ── Carrega a SquadConfig ────────────────────────────────────────────
    from flow.config.squad import SquadConfig, SquadConfigError, load_squad

    squad: SquadConfig | None = None
    squad_file = cfg.get("squad_config")
    if squad_file and not os.path.exists(squad_file):
        raise RuntimeError(
            f"deployment[{stage}]: squad_config aponta para um arquivo que não existe: {squad_file!r}"
        )
    if squad_file and os.path.exists(squad_file):
        try:
            squad = load_squad(squad_file)
        except SquadConfigError as exc:
            logger.warning("deployment[%s]: falha ao carregar squad config: %s", stage, exc)

    if squad is None:
        from flow.config.squad import _parse_squad
        raw: dict = {
            "id": cfg.get("squad_id", "default"),
            "issue_provider": issue_provider_name,
            "repos": repos,
            "workflow_template": cfg.get("workflow_template", "versao-c"),
        }
        if cfg.get("project"):
            raw["project"] = cfg["project"]
        if cfg.get("workflow_params"):
            raw["workflow_params"] = cfg["workflow_params"]
        if cfg.get("routing"):
            raw["routing"] = cfg["routing"]
        try:
            squad = _parse_squad(raw)
        except SquadConfigError as exc:
            logger.error("deployment[%s]: squad config inválida: %s", stage, exc)

    provider = provider_for(issue_provider_name)

    from flow.scan.scanner import SquadScanConfig
    if squad is not None:
        scan_cfg = SquadScanConfig(
            squad_id=squad.id,
            issue_provider=squad.issue_provider,
            projects=tuple(squad.projects),
            repos=squad.repos,
        )
    else:
        scan_cfg = SquadScanConfig(
            squad_id=cfg.get("squad_id", "default"),
            issue_provider=issue_provider_name,
            projects=tuple(repos),
            repos=frozenset(repos),
        )

    conn: sqlite3.Connection = open_cache(scan_cfg.squad_id)

    try:
        scan_results = scan_candidates(scan_cfg, provider, conn)
    except Exception as exc:
        logger.error("deployment[%s]: erro no scan: %s", stage, exc)
        conn.close()
        from kiro_crew.cron import Skip  # type: ignore[import]
        raise Skip() from exc

    # ── Rastreia running_since no cache (detecção de sessão morta) ────────
    from datetime import UTC

    _now_iso_stage = __import__("datetime").datetime.now(tz=UTC).isoformat()
    from flow.domain.state import Modifier, State

    for _r in scan_results:
        if Modifier.RUNNING in _r.modifiers:
            set_running_since(conn, _r.item.key, _now_iso_stage)
        else:
            clear_running_since(conn, _r.item.key)

    conn.close()

    from flow.executor.executor import ActionKind, decide, resolve_template

    # Cap de concorrência por estado (mecanismo primário)
    _running_dev_count_stage = sum(
        1 for _r in scan_results
        if _r.current_state is not None
        and _r.current_state.value == "crewflow:dev"
        and Modifier.RUNNING in _r.modifiers
    )

    # Filtra pelo conjunto de ações deste estágio
    allowed_actions = _STAGE_ACTIONS[stage]

    spec_invalid: list = []
    dispatch_devs: list = []
    dispatch_reviewers: list = []
    dispatch_reworks: list = []
    conflict_resolvers: list = []  # (repo, issue) — despacha sessão de resolução de conflito
    mark_conflitos: list = []      # issues para marcar crewflow:conflito
    needs_human: list = []
    blocked_bypass: list = []
    rebranded: list = []
    merge_prs: list = []
    dead_session_candidates_stage: list = []  # issues dev+running sem sinais de vida

    for result in scan_results:
        if result.spec_valid is False:
            spec_invalid.append(result)
            continue

        needs_comment = (
            Modifier.HML_BYPASS in result.modifiers
            or (result.current_state is State.DEV)
            or (result.current_state is State.TODO and "crewflow:debt" in result.item.labels)
            or (result.current_state is State.REVIEW and Modifier.REVIEWED in result.modifiers)
            or (Modifier.REVIEW_FAIL in result.modifiers)
        )
        state_comment: str | None = None
        if needs_comment:
            import contextlib
            with contextlib.suppress(Exception):
                state_comment = provider.get_state_comment(
                    result.item.key.split("/issues/")[0].replace("https://github.com/", "")
                    or (repos[0] if repos else ""),
                    result.item.key,
                )

        pr_head_sha: str | None = None
        pr_mergeable: str | None = None
        if result.current_state is State.REVIEW:
            import contextlib
            with contextlib.suppress(Exception):
                _repo = (
                    result.item.key.split("/issues/")[0].replace("https://github.com/", "")
                    or (repos[0] if repos else "")
                )
                _issue_number = int(result.item.key.split("/issues/")[-1]) if "/issues/" in result.item.key else 0
                if _issue_number and hasattr(provider, "get_pr_for_issue"):
                    _pr = provider.get_pr_for_issue(_repo, _issue_number)
                    if _pr:
                        pr_head_sha = _pr.get("headRefOid") or _pr.get("headRefName")
                        pr_mergeable = _pr.get("mergeable")  # "MERGEABLE" | "CONFLICTING" | "UNKNOWN"

        decision = decide(result, state_comment=state_comment, squad=squad, pr_head_sha=pr_head_sha, pr_mergeable=pr_mergeable)

        template = resolve_template(result, squad)
        logger.info(
            "deployment[%s]: issue=%s template=%s action=%s",
            stage, result.item.key, template, decision.action,
        )

        # Aplica filtro por estágio: só processa ações deste cron
        if decision.action.value not in allowed_actions:
            if decision.action is ActionKind.SKIP:
                continue
            # Ação de outro estágio — silêncio; o cron do estágio certo vai pegar
            logger.debug(
                "deployment[%s]: action %s ignorada (pertence a outro estágio)",
                stage, decision.action,
            )
            continue

        # Categoriza a ação
        if decision.action is ActionKind.DISPATCH_DEV:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            if not repo:
                repo = repos[0] if repos else ""
            issue = _scan_result_to_issue(result)
            dispatch_devs.append((repo, issue, decision))

        elif decision.action is ActionKind.DISPATCH_REVIEWER:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            issue = _scan_result_to_issue(result)
            dispatch_reviewers.append((repo, issue))

        elif decision.action is ActionKind.MARK_CONFLITO:
            mark_conflitos.append(result)

        elif decision.action is ActionKind.DISPATCH_CONFLICT_RESOLVER:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            if not repo:
                repo = repos[0] if repos else ""
            issue = _scan_result_to_issue(result)
            conflict_resolvers.append((repo, issue))

        elif decision.action is ActionKind.MERGE_PR:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            issue = _scan_result_to_issue(result)
            merge_prs.append((repo, issue, state_comment))

        elif decision.action is ActionKind.DISPATCH_REWORK:
            repo = result.item.key.split("/issues/")[0].replace("https://github.com/", "")
            if not repo:
                repo = repos[0] if repos else ""
            issue = _scan_result_to_issue(result)
            dispatch_reworks.append((repo, issue, state_comment))

    # ── Identifica candidatos de sessão morta para o estágio dev ─────────
    if stage == _STAGE_DEV:
        for _r_ds in scan_results:
            if (
                _r_ds.current_state is not None
                and _r_ds.current_state.value == "crewflow:dev"
                and Modifier.RUNNING in _r_ds.modifiers
            ):
                _repo_ds = _r_ds.item.key.split("/issues/")[0].replace("https://github.com/", "") or (repos[0] if repos else "")
                _num_str_ds = _r_ds.item.key.split("/issues/")[-1] if "/issues/" in _r_ds.item.key else "0"
                _num_ds = int(_num_str_ds) if _num_str_ds.isdigit() else 0
                if _num_ds and not _issue_has_active_session(_repo_ds, _num_ds, dev_root):
                    dead_session_candidates_stage.append(_r_ds)

    _log_cycle_summary(
        ctx=ctx,
        chat_id=chat_id,
        scan_total=len(scan_results),
        dispatch_dev=len(dispatch_devs),
        dispatch_reviewer=len(dispatch_reviewers),
        dispatch_rework=len(dispatch_reworks),
        conflict_resolver=len(conflict_resolvers),
        mark_conflito=len(mark_conflitos),
        merge_pr=len(merge_prs),
        notify_human=len(needs_human),
        block=len(blocked_bypass),
        rebrand=len(rebranded),
        spec_invalid=len(spec_invalid),
    )

    if not any([spec_invalid, dispatch_devs, dispatch_reviewers, dispatch_reworks,
                conflict_resolvers, mark_conflitos,
                needs_human, blocked_bypass, rebranded, merge_prs, dead_session_candidates_stage]):
        return

    if dry_run:
        _dry_run_report(
            scan_results=scan_results,
            dispatch_devs=dispatch_devs,
            dispatch_reviewers=dispatch_reviewers,
            dispatch_reworks=dispatch_reworks,
            needs_human=needs_human,
            blocked_bypass=blocked_bypass,
            rebranded=rebranded,
            merge_prs=merge_prs,
            spec_invalid=spec_invalid,
            conflict_resolvers=conflict_resolvers,
            mark_conflitos=mark_conflitos,
        )
        return

    # ── Executa as ações do estágio ──────────────────────────────────────
    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""

    if stage == _STAGE_DEV:
        _backstop_count_stage = _active_sessions()
        vagas = (max_conc - max(_running_dev_count_stage, _backstop_count_stage)) if auto else 0
        disparadas: list = []
        adiadas: list = []

        # Reabre o cache para leitura do running_since (detecção de sessão morta)
        _conn_stage_disp = open_cache(scan_cfg.squad_id)

        for repo, issue, _decision in dispatch_devs:
            if not auto:
                adiadas.append((repo, issue))
                continue
            if vagas <= 0:
                adiadas.append((repo, issue))
                continue

            # Mecanismo primário: verifica estado da issue (não lock por tempo)
            if _issue_has_active_session(repo, issue["number"], dev_root):
                logger.info(
                    "deployment[dev]: sessão ativa detectada pelo estado para %s#%s — dispatch ignorado",
                    repo, issue["number"],
                )
                adiadas.append((repo, issue))
                continue

            if not _resource_headroom_ok(ctx, max_conc):
                adiadas.append((repo, issue))
                continue
            _clean_stale_worktree(dev_root, repo, issue["number"])
            try:
                prompt_extra = squad.dispatch_prompt_extra if squad else ""
                _dispatch(ctx, repo, issue, cfg, prompt_extra=prompt_extra)
                disparadas.append((repo, issue))
                vagas -= 1
            except Exception as exc:
                logger.error("deployment[dev]: erro ao despachar %s: %s", issue.get("number"), exc)
                adiadas.append((repo, issue))

        _conn_stage_disp.close()

        # ── Detecção e recuperação de sessões mortas no estágio dev ─────────
        for result in dead_session_candidates_stage:
            _repo_dead_s = result.item.key.split("/issues/")[0].replace("https://github.com/", "") or (repos[0] if repos else "")
            _num_str_s = result.item.key.split("/issues/")[-1] if "/issues/" in result.item.key else "0"
            _num_dead_s = int(_num_str_s) if _num_str_s.isdigit() else 0
            if not _num_dead_s:
                continue
            _conn_dead_s = open_cache(scan_cfg.squad_id)
            try:
                _rs_iso_s = get_running_since(_conn_dead_s, result.item.key)
            finally:
                _conn_dead_s.close()
            _rs_secs_s: float | None = None
            if _rs_iso_s:
                try:
                    import datetime as _dt
                    _rs_secs_s = _dt.datetime.fromisoformat(_rs_iso_s).timestamp()
                except Exception:
                    pass
            if _is_dead_session(_repo_dead_s, _num_dead_s, dev_root, _rs_secs_s):
                _conn_rec_s = open_cache(scan_cfg.squad_id)
                try:
                    _recover_dead_session(ctx, _repo_dead_s, _num_dead_s, provider, chat_id, _conn_rec_s)
                finally:
                    _conn_rec_s.close()

        if auto and disparadas:
            linhas = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in disparadas)
            extra = ""
            if adiadas:
                fila = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in adiadas)
                extra = f"\n\nNA FILA:\n{fila}"
            ctx.notify(  # type: ignore[attr-defined]
                f"KiroCrew Flow [dev]: disparei sessão(ões) one-shot.{vm}\n{linhas}{extra}"
            )
        elif auto and adiadas:
            fila = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in adiadas)
            ctx.notify(  # type: ignore[attr-defined]
                f"KiroCrew Flow [dev]: {len(adiadas)} issue(s) na fila (limite cheio).{vm}\n{fila}"
            )
        elif not auto and dispatch_devs:
            blocos = "\n".join(
                f"  - {repo}#{issue['number']}: {issue['title']}"
                for repo, issue, _ in dispatch_devs
            )
            ctx.notify(  # type: ignore[attr-defined]
                f"KiroCrew Flow [dev] (Fase 1): {len(dispatch_devs)} issue(s) prontas.{vm}\n{blocos}"
            )

    elif stage == _STAGE_REVIEWER:
        for repo, issue in dispatch_reviewers:
            issue_number = issue["number"]
            if _reviewer_has_active(repo, issue_number):
                logger.info(
                    "deployment[reviewer]: reviewer já ativo para %s#%s — dispatch ignorado",
                    repo, issue_number,
                )
                continue
            try:
                _dispatch_reviewer(ctx, repo, issue, cfg)
            except Exception as exc:
                logger.error(
                    "deployment[reviewer]: erro ao despachar reviewer para %s#%s: %s",
                    repo, issue_number, exc,
                )

        # Aplica crewflow:conflito nas PRs com conflito detectado
        if mark_conflitos:
            for result in mark_conflitos:
                try:
                    _repo_mc = result.item.key.split("/issues/")[0].replace("https://github.com/", "") or (repos[0] if repos else "")
                    current_labels = list(result.item.labels)
                    if "crewflow:conflito" not in current_labels:
                        current_labels.append("crewflow:conflito")
                    provider.set_labels(_repo_mc, result.item.key, current_labels)
                    logger.info("deployment[reviewer]: crewflow:conflito aplicado em %s", result.item.key)
                except Exception as exc:
                    logger.error("deployment[reviewer]: erro ao aplicar conflito em %s: %s", result.item.key, exc)
            linhas_mc = "\n".join(f"  - {r.item.key}: {r.item.title}" for r in mark_conflitos)
            ctx.notify(  # type: ignore[attr-defined]
                f"KiroCrew Flow [reviewer]: {len(mark_conflitos)} PR(s) com conflito de merge detectado — "
                f"crewflow:conflito aplicado.{vm}\n{linhas_mc}"
            )

    elif stage == _STAGE_MERGE:
        if merge_prs:
            _execute_auto_merges(ctx, merge_prs, chat_id, provider)

    elif stage == _STAGE_CONFLITO:
        # ── Despacha sessões de resolução de conflito de merge ───────────
        for repo, issue in conflict_resolvers:
            issue_number_cr = issue["number"]
            if not auto:
                ctx.notify(  # type: ignore[attr-defined]
                    f"KiroCrew Flow [conflito] (Fase 1): conflito pendente — "
                    f"{repo}#{issue_number_cr}: {issue['title']}.{vm}\n"
                    f"  Ative auto_dispatch para despachar o resolvedor de conflito automaticamente."
                )
                continue
            if _conflict_resolver_has_active(repo, issue_number_cr):
                logger.info(
                    "deployment[conflito]: resolvedor de conflito já ativo para %s#%s — dispatch ignorado",
                    repo, issue_number_cr,
                )
                continue
            branch_cr = f"feat/issue-{issue_number_cr}"
            pr_number_cr: int | None = None
            try:
                import subprocess as _sp2
                _pr_cr_res = _sp2.run(
                    ["gh", "pr", "list", "--repo", repo, "--head", branch_cr,
                     "--state", "open", "--json", "number"],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if _pr_cr_res.returncode == 0:
                    _prs_cr = json.loads(_pr_cr_res.stdout or "[]")
                    if _prs_cr:
                        pr_number_cr = int(_prs_cr[0]["number"])
            except Exception as exc_cr:
                logger.warning(
                    "deployment[conflito]: erro ao localizar PR para conflict resolver %s#%s: %s",
                    repo, issue_number_cr, exc_cr,
                )
            if pr_number_cr is None:
                ctx.notify(  # type: ignore[attr-defined]
                    f"KiroCrew Flow [conflito]: crewflow:conflito mas PR não localizado — "
                    f"{repo}#{issue_number_cr}.{vm}"
                )
                continue
            try:
                prompt_extra_cr = squad.dispatch_prompt_extra if squad else ""
                _dispatch_conflict_resolver(ctx, repo, issue, pr_number_cr, cfg,
                                            prompt_extra=prompt_extra_cr)
            except Exception as exc:
                logger.error(
                    "deployment[conflito]: erro ao despachar conflict resolver para %s#%s: %s",
                    repo, issue_number_cr, exc,
                )

        # ── Despacha sessões de re-trabalho pós-review ───────────────────
        for repo, issue, state_comment_rework in dispatch_reworks:
            issue_number = issue["number"]
            if not auto:
                ctx.notify(  # type: ignore[attr-defined]
                    f"KiroCrew Flow [conflito] (Fase 1): re-trabalho pendente — "
                    f"{repo}#{issue_number}: {issue['title']}.{vm}\n"
                    f"  Reviewer pediu mudanças. Ative auto_dispatch para despachar automaticamente."
                )
                continue
            if _rework_has_active(repo, issue_number):
                logger.info(
                    "deployment[conflito]: sessão de re-trabalho já ativa para %s#%s — dispatch ignorado",
                    repo, issue_number,
                )
                continue

            branch = f"feat/issue-{issue_number}"
            pr_number_rework: int | None = None
            try:
                import subprocess as _sp
                _pr_res = _sp.run(
                    ["gh", "pr", "list", "--repo", repo, "--head", branch,
                     "--state", "open", "--json", "number"],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if _pr_res.returncode == 0:
                    _prs = json.loads(_pr_res.stdout or "[]")
                    if _prs:
                        pr_number_rework = int(_prs[0]["number"])
            except Exception as exc_pr:
                logger.warning(
                    "deployment[conflito]: erro ao localizar PR para rework %s#%s: %s",
                    repo, issue_number, exc_pr,
                )

            if pr_number_rework is None:
                ctx.notify(  # type: ignore[attr-defined]
                    f"KiroCrew Flow [conflito]: re-trabalho pendente mas PR não localizado — "
                    f"{repo}#{issue_number}.{vm}"
                )
                continue

            from flow.audit.state_comment import get_review_iterations_from_comment
            iteration = get_review_iterations_from_comment(state_comment_rework) + 1
            try:
                prompt_extra_rework = squad.dispatch_prompt_extra if squad else ""
                _dispatch_rework(ctx, repo, issue, pr_number_rework, iteration, cfg,
                                 prompt_extra=prompt_extra_rework)
            except Exception as exc:
                logger.error(
                    "deployment[conflito]: erro ao despachar rework para %s#%s: %s",
                    repo, issue_number, exc,
                )


def run_dev(ctx: object) -> None:
    """Entrypoint do cron de implementação.

    Processa issues em ``crewflow:todo`` e despacha sessões one-shot de dev.
    Ideal com um modelo forte (ex: sonnet-4.5) e intervalo de 600s.

    Configure o modelo via ``stage_models.dev`` na deployment.config.yaml.

    Registro (uma vez):
        cron_add(name="crewflow-dev",
                 script="~/.kiro/crew/crons/deployment.py:run_dev",
                 every=600)
    """
    _run_stage(ctx, _STAGE_DEV)


def run_reviewer(ctx: object) -> None:
    """Entrypoint do cron de code review.

    Processa PRs em ``crewflow:review`` (sem ``crewflow:reviewed``) e
    despacha sessões one-shot do kiro-reviewer.
    Ideal com um modelo mais rápido e intervalo de 300s.

    Configure o modelo via ``stage_models.reviewer`` na deployment.config.yaml.

    Registro (uma vez):
        cron_add(name="crewflow-reviewer",
                 script="~/.kiro/crew/crons/deployment.py:run_reviewer",
                 every=300)
    """
    _run_stage(ctx, _STAGE_REVIEWER)


def run_merge(ctx: object) -> None:
    """Entrypoint do cron de merge.

    Processa PRs aprovados (``crewflow:review-ok``) e executa o merge squash
    automático. Intervalo curto recomendado: 120s.

    Configure o modelo via ``stage_models.merge`` na deployment.config.yaml.

    Registro (uma vez):
        cron_add(name="crewflow-merge",
                 script="~/.kiro/crew/crons/deployment.py:run_merge",
                 every=120)
    """
    _run_stage(ctx, _STAGE_MERGE)


def run_conflito(ctx: object) -> None:
    """Entrypoint do cron de re-trabalho (conflito pós-review).

    Processa issues com ``crewflow:review-fail`` e despacha sessões
    one-shot de rework para aplicar os pedidos do reviewer na mesma PR.
    Intervalo recomendado: 300s.

    Configure o modelo via ``stage_models.conflito`` na deployment.config.yaml.

    Registro (uma vez):
        cron_add(name="crewflow-conflito",
                 script="~/.kiro/crew/crons/deployment.py:run_conflito",
                 every=300)
    """
    _run_stage(ctx, _STAGE_CONFLITO)
