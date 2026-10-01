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

    run_dev(ctx)       — issues flow:develop-waiting → dispatch dev (modelo mais forte)
    run_reviewer(ctx)  — PRs flow:review-waiting → dispatch reviewer (modelo mais rápido)
    run_merge(ctx)     — flow:review-approved / flow:qa-approved → merge squash
    run_conflito(ctx)  — flow:review-refused → gate humano + flow:merge-conflict → resolução

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
import subprocess
import sys
import threading as _threading

logger = logging.getLogger(__name__)

# ── Adiciona o diretório raiz do repo ao path para importar flow/ ─────────
# Necessário porque o cron do Kiro Crew executa o arquivo diretamente e
# flow/ não está instalado como pacote no Python do sistema.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
# Quando instalado em ~/.kiro/crew/crons/, _REPO_ROOT aponta para ~/.kiro/crew/
# onde flow/ não existe. Adicionamos o caminho real do repo:
_FLOW_ROOT = "/home/elias/dev/kirocrew-flow"
if _FLOW_ROOT not in sys.path:
    sys.path.insert(0, _FLOW_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


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


from flow.domain.state import State  # noqa: E402
from flow.ports.issue_provider import provider_for  # noqa: E402
from flow.prompts.loader import PromptRenderError, render_prompt  # noqa: E402

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


def _select_squad_for_repos(squads: list, repos: list[str]):
    """Seleciona, dentre as squads carregadas de ``squads_dir``, aquela cujos
    repos/projetos intersectam os ``repos`` configurados no deployment (#311).

    O deployment dirige UMA squad por tick/estágio. Quando ``squads_dir`` tem
    vários arquivos, escolhemos a squad cujos ``projects``/``repos`` casam com
    ao menos um repo de ``cfg['repos']`` — o critério de casamento reusa
    ``_normalize_repo_identifier`` para comparar identificadores canônicos
    (owner/repo), de modo que urls completas e formas ``owner/repo`` casem.

    Regra de resolução:
      - 0 squads carregadas → ``None`` (o chamador cai para squad_config/inline);
      - exatamente 1 squad carregada → usa ela (mesmo sem casar repos, para
        manter o comportamento simples de "um squads_dir, uma squad");
      - várias squads → a primeira cujos repos/projetos intersectam ``repos``;
        se nenhuma casar, ``None``.
    """
    from flow.config.squad import _normalize_repo_identifier

    if not squads:
        return None
    if len(squads) == 1:
        return squads[0]

    wanted = {_normalize_repo_identifier(str(r)) for r in repos}
    for squad in squads:
        candidates = {
            _normalize_repo_identifier(str(p)) for p in squad.projects
        } | {_normalize_repo_identifier(str(r)) for r in squad.repos}
        if wanted & candidates:
            return squad
    return None


def _load_single_flow_squad(cfg: dict):
    """Carrega a SquadConfig para o deployment, com a ordem de seleção do #311.

    Ordem de precedência (documentada aqui para run()/_run_stage()/single-flow
    compartilharem UM único caminho):

      1. ``squads_dir`` (diretório) — preferido quando setado e existente:
         carrega TODAS as squads via ``load_squads_dir`` e seleciona a que
         casa com ``cfg['repos']`` (ver ``_select_squad_for_repos``). Um
         ``squads_dir`` setado mas inexistente é ERRO (RuntimeError), espelhando
         a mensagem de ``squad_config`` não encontrado.
      2. ``squad_config`` (arquivo único) — fallback/compatibilidade.
      3. inline — construída a partir da config legada (repos/issue_provider/...).

    Retorna a SquadConfig (com os globais já populados) ou ``None`` quando nem
    squads_dir nem squad_config produzem squad e a construção inline falha.
    """
    from flow.config.squad import (
        SquadConfig,
        SquadConfigError,
        load_squad,
        load_squads_dir,
    )

    repos: list[str] = cfg.get("repos") or []
    issue_provider_name: str = cfg.get("issue_provider", "github")
    squad: SquadConfig | None = None

    # ── (1) squads_dir: diretório de squads do app instalado (#311) ────────
    squads_dir = cfg.get("squads_dir")
    if squads_dir and not os.path.isdir(squads_dir):
        raise RuntimeError(
            f"deployment: squads_dir aponta para um diretório que não existe: {squads_dir!r}"
        )
    if squads_dir and os.path.isdir(squads_dir):
        try:
            squads = load_squads_dir(squads_dir)
            squad = _select_squad_for_repos(squads, repos)
            if squad is not None:
                logger.info(
                    "deployment: squad carregada de squads_dir %s (%s)",
                    squads_dir, squad.id,
                )
        except SquadConfigError as exc:
            logger.warning("deployment: falha ao carregar squads_dir: %s", exc)

    # ── (2) squad_config: arquivo único (fallback/compat) ──────────────────
    if squad is None:
        squad_file = cfg.get("squad_config")
        if squad_file and not os.path.exists(squad_file):
            raise RuntimeError(
                f"deployment: squad_config aponta para um arquivo que não existe: {squad_file!r}"
            )
        if squad_file and os.path.exists(squad_file):
            try:
                squad = load_squad(squad_file)
                logger.info(
                    "deployment: squad carregada de %s (%s)", squad_file, squad.id
                )
            except SquadConfigError as exc:
                logger.warning("deployment: falha ao carregar squad config: %s", exc)

    # ── (3) inline: construída a partir da config legada ───────────────────
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

    # ── Popula os defaults globais na SquadConfig (issue #264) ─────────────
    if squad is not None:
        squad.global_auto_dispatch = bool(cfg.get("auto_dispatch", False))
        squad.global_auto_merge = bool(squad.workflow_params.auto_merge_on_approve)

    return squad


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
    # Lock criado em subdiretório separado (não em _sessdir()) para não
    # interferir com os arquivos de sessão que o gateway cria.
    backstop_dir = os.path.join(_sessdir(), "backstop")
    os.makedirs(backstop_dir, exist_ok=True)
    lock_path = os.path.join(backstop_dir, f"dispatch-{short}-{issue_number}.lock")

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


def _post_agent_session(
    ctx: object,
    message: str,
    *,
    slot: str,
    cfg: dict,
) -> bool:
    """Despacha uma sessão de agente (fire-and-forget).

    Fluxo de 2 calls via loopback interno (X-Internal-Secret):

    1. POST /api/chat/slots  → registra o slot no estado do gateway (idempotente).
    2. POST /api/chat        → envia a mensagem com slot_key no body (Kiro Crew 0.7.1+).

    Kiro Crew 0.7.1: o campo ``slot_key`` substituiu o header ``X-Session-Key``
    no body do POST /api/chat. O header foi removido.

    Retorna True quando os dois POSTs foram bem-sucedidos, False caso contrário.
    Exceções são engolidas/logadas (fire-and-forget).
    """
    import urllib.request as _u

    port = getattr(ctx, "_port", None)
    # ctx._port fica fixado no registro do cron — se o gateway mudou de porta
    # após o registro, o valor fica desatualizado. Detectar dinamicamente via socket.
    _sock_pattern = os.path.expanduser("~/.kiro/crew/dashboard-*.sock")
    _socks = glob.glob(_sock_pattern)
    if _socks:
        import re as _re
        _m = _re.search(r"dashboard-(\d+)\.sock", _socks[0])
        if _m:
            port = int(_m.group(1))
    if not port:
        port = 5478  # fallback final
    # Precedência do secret interno do gateway (issue #263):
    #   1. ~/.kiro/crew/run/gateway-{port}.secret — sempre regenerado no restart
    #   2. ~/.kiro/crew/.local_secret            — fallback (pode ficar stale)
    #   3. ctx._secret                            — último recurso (fixado no registro)
    # O .local_secret e o ctx._secret ficam desatualizados após restart do
    # gateway, causando 403 no dispatch; o run/gateway-{port}.secret vence.
    secret = ""
    _gateway_secret_path = os.path.expanduser(
        f"~/.kiro/crew/run/gateway-{port}.secret"
    )
    if os.path.exists(_gateway_secret_path):
        with open(_gateway_secret_path) as _f:
            secret = _f.read().strip()
    if not secret:
        _local_secret_path = os.path.expanduser("~/.kiro/crew/.local_secret")
        if os.path.exists(_local_secret_path):
            with open(_local_secret_path) as _f:
                secret = _f.read().strip()
    if not secret:
        secret = getattr(ctx, "_secret", "")
    if not port:
        logger.error(
            "deployment: dispatch abortado (slot %s) — porta do gateway não disponível.",
            slot,
        )
        return False

    agent = cfg.get("agent") or "kirocrew"

    # ── Step 1: criar o slot (/api/chat/slots) ─────────────────────────────
    slot_body = json.dumps({"name": slot, "agent": agent, "memory_mode": "temporary"}).encode()
    slot_req = _u.Request(
        f"http://localhost:{port}/api/chat/slots",
        data=slot_body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": secret,
        },
        method="POST",
    )
    try:
        with _u.urlopen(slot_req, timeout=10) as resp:
            resp.read(1)
    except _u.HTTPError as exc:
        if exc.code == 409:
            # Slot já existe (tentativa anterior falhou após criar o slot).
            # Reutilizar — prosseguir para o step 2.
            logger.warning("deployment: slot %s já existe (409) — reutilizando para envio", slot)
        else:
            logger.error("deployment: falha ao criar slot %s: HTTP %s", slot, exc.code)
            return False
    except Exception as exc:
        logger.error("deployment: falha ao criar slot %s: %s", slot, exc)
        return False

    # ── Step 2: enviar a mensagem (/api/chat) ──────────────────────────────
    # Kiro Crew 0.7.1: slot_key vai no body (não mais X-Session-Key no header).
    chat_body = json.dumps({
        "slot_key": slot,
        "message": message,
        "agent": agent,
    }).encode()
    chat_req = _u.Request(
        f"http://localhost:{port}/api/chat",
        data=chat_body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": secret,
        },
        method="POST",
    )
    try:
        with _u.urlopen(chat_req, timeout=12) as resp:
            resp.read(1)
    except _u.HTTPError as exc:
        # HTTP 4xx/5xx: logar o corpo da resposta (antes era silencioso — issue #267)
        # e limpar o slot órfão criado no Step 1 para não aparecer como
        # 'New Session...' vazia no sidebar.
        body = _read_error_body(exc)
        logger.error(
            "deployment: falha ao despachar sessão (slot %s): HTTP %s — %s",
            slot, exc.code, body,
        )
        _delete_orphan_slot(port, secret, slot)
        return False
    except Exception as exc:
        logger.error("deployment: falha ao despachar sessão (slot %s): %s", slot, exc)
        _delete_orphan_slot(port, secret, slot)
        return False
    return True


def _read_error_body(exc: object) -> str:
    """Extrai o corpo da resposta de um ``HTTPError`` de forma defensiva.

    O corpo é essencial para diagnosticar falhas do Step 2 (ex.:
    ``member_identity_unavailable`` intermitente — issue #267). Nunca lança:
    qualquer erro na leitura vira ``"<sem corpo>"``.
    """
    try:
        raw = exc.read()  # type: ignore[attr-defined]
    except Exception:
        return "<sem corpo>"
    if not raw:
        return "<sem corpo>"
    try:
        return raw.decode("utf-8", "replace").strip()
    except Exception:
        return repr(raw)


def _delete_orphan_slot(port: int, secret: str, slot: str) -> None:
    """Deleta o slot criado no Step 1 quando o Step 2 (/api/chat) falha.

    Sem esse cleanup, o slot fica órfão (criado em ``/api/chat/slots`` mas sem
    nenhuma mensagem) e aparece como 'New Session...' vazia no sidebar do
    dashboard (issue #267). É best-effort/fail-safe: qualquer erro é logado e
    engolido — a falha do dispatch já foi reportada pelo caller.
    """
    import urllib.request as _u
    from urllib.parse import quote

    del_req = _u.Request(
        f"http://localhost:{port}/api/chat/slots/{quote(slot, safe='')}",
        headers={"X-Internal-Secret": secret},
        method="DELETE",
    )
    try:
        with _u.urlopen(del_req, timeout=10) as resp:
            resp.read(1)
        logger.info("deployment: slot órfão %s removido após falha do Step 2", slot)
    except _u.HTTPError as exc:
        if exc.code == 404:
            # Slot já não existe — nada a limpar.
            return
        logger.warning(
            "deployment: falha ao remover slot órfão %s: HTTP %s", slot, exc.code
        )
    except Exception as exc:
        logger.warning("deployment: falha ao remover slot órfão %s: %s", slot, exc)


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
    "comente, marque `flow:blocked`, avise e ENCERRE.\n"
    "3. Marque `flow:develop-running` e REMOVA `flow:develop-waiting`. NÃO faça `git clone`. Use o clone em "
    "`{{dev_root}}/{{repo_short}}` como base e crie um WORKTREE ISOLADO.\n"
    "   A branch base é a DEFAULT DO REPO — descubra, não presuma:\n"
    "   `BASE=$(gh repo view {{repo}} --json defaultBranchRef --jq .defaultBranchRef.name)`\n"
    "   `cd {{dev_root}}/{{repo_short}} && git fetch origin && git worktree add -b "
    "feat/issue-{{issue_number}} {{worktree_path}} \"origin/$BASE\"`\n"
    "   Para trocar o estado, use SEMPRE a forma atômica que remove todos os estados anteriores:\n"
    "   `gh issue edit {{issue_number}} --repo {{repo}} --add-label \"flow:develop-running\" --remove-label \"flow:develop-waiting\"`\n"
    "   Trabalhe DENTRO do worktree; remova-o ao fim. NUNCA toque em outros worktrees.\n"
    "4. REBASE ANTES DE EDITAR — minimize a janela de divergência:\n"
    "   `cd {{worktree_path}} && git fetch origin && git rebase origin/{{base_branch}}`\n"
    "   Faça isso imediatamente antes de editar qualquer arquivo. Se o rebase conflitar, resolva antes de continuar.\n"
    "5. Implemente EXATAMENTE o escopo — nada além.\n"
    "6. DOCS: atualize README, steerings e docs/ se a mudança afeta comportamento, "
    "arquitetura ou convenções. Não atualize se a mudança for puramente interna (bugfix, refactor).\n"
    "7. **VALIDAÇÃO OBRIGATÓRIA — rode ANTES de abrir PR.** Se o repo for `eliasrosa/kirocrew-flow`, execute exatamente:\n"
    "   ```bash\n"
    "   python3 -m ruff check flow/\n"
    "   python3 -m mypy flow/ --ignore-missing-imports\n"
    "   python3 -m pytest flow/tests/ --cov=flow --cov-fail-under=75\n"
    "   ```\n"
    "   Para outros repos, descubra os comandos via README/Makefile/pyproject — **não presuma**.\n"
    "   Se qualquer check falhar e você não conseguir corrigir, marque `flow:blocked` e ENCERRE. "
    "**Não abra PR com CI vermelho.**\n"
    "8. Abra PR com 'Closes #{{issue_number}}' e troque a label para `flow:review-waiting` REMOVENDO `flow:develop-running`. "
    "Após abrir o PR, ATUALIZE o título da sessão adicionando o número do PR: "
    "`{{repo_short}} #{{issue_number}} #<N-PR>: {{issue_title}}`. "
    "**NUNCA mergeie. NUNCA faça deploy.** Ambos são ações humanas manuais.\n"
    "   Use SEMPRE a forma atômica que remove todos os estados anteriores:\n"
    "   `gh issue edit {{issue_number}} --repo {{repo}} --add-label \"flow:review-waiting\" --remove-label \"flow:develop-running,flow:develop-waiting\"`\n"
    "9. Ao terminar: {{notify_step}}\n\n"
    "   ENCERRE.\n\n"
    "{{vault_step}}\n\n"
    "### Regras críticas\n\n"
    "- UMA passada. Terminou, acabou. NÃO entre em loop.\n"
    "- NUNCA mergeie. NUNCA faça deploy.\n"
    "- Se bloquear, marque `flow:blocked`, avise, e pare.\n\n"
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

    # Descobre a branch base do repo para o passo de rebase
    base_branch = "main"
    try:
        _bb = subprocess.run(
            ["gh", "repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if _bb.returncode == 0:
            base_branch = _bb.stdout.strip() or "main"
    except Exception:
        pass

    try:
        return render_prompt(
            "develop_waiting",
            fallback=_DEV_PROMPT_FALLBACK,
            repo=repo,
            repo_short=short,
            issue_number=str(issue["number"]),
            issue_title=issue["title"],
            issue_url=issue["url"],
            session_title=session_title,
            dev_root=dev_root,
            worktree_path=worktree,
            base_branch=base_branch,
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
    _post_agent_session(ctx, message, slot=slot, cfg=cfg)


# ── Estágios de especificação single-flow: briefing e planning-specs ─────
#
# Sessões one-shot que NÃO tocam código: leem a task, criam/preenchem as
# sub-tasks e transicionam a label. Espelham o padrão de _dispatch (guard de
# issue CLOSED + backstop lock atômico + POST /api/chat), mas sem worktree,
# rebase ou PR — a spec vive nas sub-tasks da issue, não em branch.

_BRIEFING_PROMPT_FALLBACK = (
    "# {{session_title}}\n\n"
    "Sessão ONE-SHOT de briefing (single-flow) para {{repo}}#{{issue_number}} — "
    "{{issue_title}} ({{issue_url}}).\n\n"
    "Se a issue estiver CLOSED, encerre sem ação. Leia steerings/README/docs e a "
    "issue com comentários; entenda a demanda. Se vaga, marque flow:blocked e pare. "
    "Crie 2 sub-tasks fixas (Especificação, Implementação) referenciando a pai, "
    "registre os números num comentário, transicione flow:briefing → "
    "flow:planning-specs e {{notify_step}}. NÃO escreva a spec, NÃO code, NÃO abra "
    "PR. Estimativa é manual.\n\n{{prompt_extra}}"
)

_PLANNING_PROMPT_FALLBACK = (
    "# {{session_title}}\n\n"
    "Sessão ONE-SHOT de especificação (single-flow) para {{repo}}#{{issue_number}} — "
    "{{issue_title}} ({{issue_url}}).\n\n"
    "Se a issue estiver CLOSED, encerre sem ação. Releia o material e as decisões do "
    "briefing. Monte a spec padrão Kiro (requirements + design + tasks) na Sub-task 1 "
    "· Especificação. Sinalize pontos de design em aberto para o TL/PM. Peça a revisão "
    "(fechar a Sub-task 1 marca o aceite), transicione flow:planning-specs → "
    "flow:planning-review e {{notify_step}}. NÃO feche a sub-task, NÃO code, NÃO abra "
    "PR. Estimativa é manual.\n\n{{prompt_extra}}"
)


def _spec_stage_prompt(
    stage_template: str,
    fallback: str,
    repo: str,
    issue: dict,
    cfg: dict,
    prompt_extra: str = "",
) -> str:
    """Renderiza o prompt de um estágio de especificação (briefing/planning).

    Usa ``flow/prompts/<stage_template>.md`` como fonte primária, caindo no
    ``fallback`` embutido se o arquivo estiver ausente. Variável faltando →
    ``PromptRenderError`` (fail-closed), como os demais estágios.
    """
    short = repo.split("/")[-1]
    chat_id = cfg.get("notify_chat_id") or ""
    notify_step = (
        f"avise via voice_maybe (chat_id {chat_id}, intent auto) com TL;DR, "
        if chat_id else "reporte o resultado, "
    )
    session_title = f"{short} #{issue['number']}: {issue['title']}"
    return render_prompt(
        stage_template,
        fallback=fallback,
        repo=repo,
        repo_short=short,
        issue_number=str(issue["number"]),
        issue_title=issue["title"],
        issue_url=issue["url"],
        session_title=session_title,
        notify_step=notify_step,
        prompt_extra=prompt_extra.strip(),
    )


def _dispatch_spec_stage(
    ctx: object,
    repo: str,
    issue: dict,
    cfg: dict,
    *,
    stage_template: str,
    fallback: str,
    slot_prefix: str,
    prompt_extra: str = "",
) -> bool:
    """Despacha uma sessão one-shot de estágio de especificação (single-flow).

    Guard de issue CLOSED + backstop lock atômico antes do POST — mesma
    disciplina de concorrência de ``_dispatch``.

    Retorna ``True`` só quando a sessão foi de fato despachada (os dois POSTs
    de ``_post_agent_session`` OK). Retorna ``False`` em QUALQUER abort — issue
    CLOSED, backstop lock já existente, template inválido, ou POST falho — para
    que o motor ledger-driven (``tick``) NÃO avance o estágio quando nenhum
    trabalho começou (Gap A). Antes esta função devolvia ``None`` e o dispatcher
    do single-flow assumia sucesso sempre, avançando o ledger no vazio.
    """
    if _is_issue_closed(repo, issue["number"]):
        logger.info(
            "deployment[%s]: dispatch abortado — issue %s#%s está CLOSED",
            slot_prefix, repo, issue["number"],
        )
        return False

    acquired, _lock_path = _try_acquire_dispatch_lock(repo, issue["number"])
    if not acquired:
        logger.info(
            "deployment[%s]: dispatch abortado — backstop lock já existe para %s#%s",
            slot_prefix, repo, issue["number"],
        )
        return False

    slot = f"{slot_prefix}-{repo.split('/')[-1]}-{issue['number']}"
    try:
        message = _spec_stage_prompt(
            stage_template, fallback, repo, issue, cfg, prompt_extra=prompt_extra,
        )
    except PromptRenderError as exc:
        logger.error(
            "deployment[%s]: dispatch abortado — template %r inválido para %s#%s: %s",
            slot_prefix, stage_template, repo, issue["number"], exc,
        )
        return False
    return _post_agent_session(ctx, message, slot=slot, cfg=cfg)


def _dispatch_briefing(
    ctx: object, repo: str, issue: dict, cfg: dict, prompt_extra: str = "",
) -> bool:
    """Despacha a sessão one-shot de briefing (flow:briefing, single-flow).

    Retorna ``True`` só quando a sessão foi despachada; ``False`` em abort.
    """
    return _dispatch_spec_stage(
        ctx, repo, issue, cfg,
        stage_template="briefing",
        fallback=_BRIEFING_PROMPT_FALLBACK,
        slot_prefix="briefing",
        prompt_extra=prompt_extra,
    )


def _dispatch_planning(
    ctx: object, repo: str, issue: dict, cfg: dict, prompt_extra: str = "",
) -> bool:
    """Despacha a sessão one-shot de especificação (flow:planning-specs, single-flow).

    Retorna ``True`` só quando a sessão foi despachada; ``False`` em abort.
    """
    return _dispatch_spec_stage(
        ctx, repo, issue, cfg,
        stage_template="planning_specs",
        fallback=_PLANNING_PROMPT_FALLBACK,
        slot_prefix="planning",
        prompt_extra=prompt_extra,
    )


# ── Monitor zero-token da issue (issue #211) ─────────────────────────────

def _create_issue_monitor(repo: str, issue_number: int) -> None:
    """Cria (idempotente) um cron zero-token que monitora a issue despachada.

    Nasce como efeito colateral do dispatch de dev: notifica no canal do
    usuário quando a PR abre, quando a issue fecha (PR mergeada) ou quando a
    sessão parece ter travado — sem gastar tokens (Python puro, só ``gh``).
    O script alvo é ``deployment/flow/watch_issue.py:check``.

    Idempotente por nome (``watch-<repo_short>-<N>``): um re-dispatch da mesma
    issue não cria um segundo monitor. Fail-safe: qualquer erro é logado e
    engolido — criar o monitor NUNCA pode derrubar o dispatch já concluído.
    """
    short = repo.split("/")[-1]
    name = f"watch-{short}-{issue_number}"
    try:
        from kiro_crew.config.paths import config_dir  # type: ignore[import]
        from kiro_crew.cron import CronService  # type: ignore[import]

        svc = CronService(base_dir=config_dir())
        job = svc.add_job_if_absent(
            lambda j: getattr(j, "name", "") == name,
            name=name,
            message=f"{repo}#{issue_number}",
            every_secs=180,
            script="~/.kiro/crew/crons/deployment/flow/watch_issue.py:check",
            persistent_session=False,
            minimal_context=True,
            hide_in_chat=True,
            created_by="flow-dev",
        )
        if job is None:
            logger.info(
                "deployment[dev]: monitor '%s' já existe — não recriado", name
            )
        else:
            logger.info("deployment[dev]: monitor zero-token '%s' criado", name)
    except Exception as exc:
        logger.warning(
            "deployment[dev]: falha ao criar monitor '%s' (dispatch preservado): %s",
            name, exc,
        )


# ── Conversão ScanResult → formato legado do dispatch ────────────────────

def _ledger_advance(cfg: dict, squad_id: str, repo: str, issue: dict, new_stage: str) -> None:
    """Registra no RunLedger a transição da task para ``new_stage`` (fail-safe).

    Só age quando ``single_flow`` está ligado. O ledger é a fonte de verdade do
    "onde a task está" no modo single-flow; qualquer erro é logado e engolido
    — o ledger NUNCA pode derrubar a transição de label já aplicada.
    """
    if not bool(cfg.get("single_flow", False)):
        return
    try:
        from flow.domain.run_ledger import SqliteRunLedger

        task_key = issue.get("url") or f"{repo}#{issue.get('number')}"
        ledger = SqliteRunLedger(squad_id)
        try:
            # claim é idempotente: prende a task se o slot estiver livre, ou
            # confirma que já é a task ativa. advance registra o novo estágio.
            ledger.claim(task_key, repo, new_stage)
            ledger.advance(task_key, new_stage)
        finally:
            ledger.close()
    except Exception as exc:
        logger.warning(
            "deployment: falha ao registrar transição no RunLedger para %s#%s (transição preservada): %s",
            repo, issue.get("number"), exc,
        )


# ── Shadow mode: estado implícito em paralelo com labels ─────────────────

def _collect_implicit_state(
    result: object,
    provider: object,
    project: str,
) -> object:
    """Coleta evidências externas e deriva o estado implícito de uma issue.

    Faz chamadas de I/O (branch check + PR + reviews) e delega a lógica pura
    para ``implicit_state()`` em flow/scan/scanner.py.

    Retorna ImplicitState ou None se ocorrer erro ao coletar as evidências (fail-safe).
    """
    import re as _re

    from flow.scan.scanner import ScanResult as _ScanResult
    from flow.scan.scanner import implicit_state as _implicit_state

    _result: _ScanResult = result  # type: ignore[assignment]

    # Extrai o número da issue da key
    m = _re.search(r"[#\-/](\d+)$", _result.item.key)
    if not m:
        return None
    issue_number = int(m.group(1))

    # Verifica se a issue está fechada
    issue_closed = False
    try:
        raw = provider.get_work_item(project, _result.item.key)  # type: ignore[attr-defined]
        issue_closed = (raw.get("state") or "").lower() == "closed"
    except Exception:
        pass

    # Verifica se a branch canônica existe
    branch_name = f"feat/issue-{issue_number}"
    branches: list[str] = []
    try:
        from flow.adapters import github_client as _gh
        if _gh.get_branch_exists(project, branch_name):
            branches = [branch_name]
    except Exception:
        pass

    # Busca PR aberta e seus reviews
    prs: list[dict] = []
    try:
        from flow.adapters import github_client as _gh2
        pr = _gh2.get_pr_for_issue(project, issue_number)
        if pr:
            pr_number = pr.get("number")
            reviews: list[dict] = []
            if pr_number:
                import contextlib as _ctxlib
                with _ctxlib.suppress(Exception):
                    reviews = _gh2.get_pr_reviews(project, int(pr_number))
            pr_entry = dict(pr)
            pr_entry["state"] = "open"
            pr_entry["reviews"] = reviews
            prs = [pr_entry]
    except Exception:
        pass

    try:
        return _implicit_state(
            issue_closed=issue_closed,
            branches=branches,
            prs=prs,
            issue_number=issue_number,
        )
    except Exception:
        return None


_IMPLICIT_TO_EXPLICIT: dict = {}  # preenchido abaixo, após imports


def _build_implicit_to_explicit_map() -> dict:
    """Mapeia ImplicitState → State equivalente para comparação."""
    from flow.domain.state import State
    from flow.scan.scanner import ImplicitState

    return {
        ImplicitState.TODO:      State.DEVELOP_WAITING,
        ImplicitState.DEV:       State.DEVELOP_RUNNING,
        ImplicitState.REVIEW:    State.REVIEW_WAITING,
        ImplicitState.REVIEW_OK: State.REVIEW_APPROVED,
        ImplicitState.DONE:      State.DONE,
    }


def run_single_flow(ctx: object) -> None:
    """Entrypoint ÚNICO do modo single-flow (frente 5).

    Uma só cron (recomendado: every=30s, zero-token) que orquestra o fluxo
    inteiro. A cada tick percorre todos os estágios do single-flow em ordem,
    delegando a ``_run_stage`` — que faz scan determinístico do estado (custo
    zero de token) e só aciona o LLM quando há uma sessão a despachar.

    A concorrência "uma task por vez" é imposta por ``max_concurrent: 1`` na
    config e pelo RunLedger (fonte de verdade do estágio atual da task). Um
    estágio que levante exceção é logado e não interrompe os demais — o tick
    é resiliente por estágio.

    Só tem efeito com ``single_flow: true`` na config; com a flag desligada,
    cada ``_run_stage`` roda o scan mas o executor não emite as ações de
    briefing/planning/advance, então nada single-flow é despachado.

    Registro (uma vez):
        cron_add(name="flow-single",
                 script="~/.kiro/crew/crons/deployment.py:run_single_flow",
                 every=60)
    """
    if not bool(_load_config().get("single_flow", False)):
        # Flag desligada: o motor ledger-driven não opera. Evita qualquer I/O.
        logger.info("deployment[single-flow]: single_flow desligado — tick inócuo")
        return
    try:
        _single_flow_tick(ctx)
    except Exception as exc:  # resiliência do tick
        logger.error("deployment[single-flow]: tick falhou: %s", exc)


# ── Motor ledger-driven do single-flow (frente 7) ────────────────────────────
#
# O estado da task vive no RunLedger (SQLite local), NÃO nas labels do GitHub.
# O tick lê a task ativa (1 query local), lê o estado real da issue (1 chamada
# de rede) e a empurra estágio a estágio. Custo O(1) por tick — some o scan que
# varria 16 estados x 8 estágios (~128 chamadas gh) do modelo anterior.


def _resolve_provider_for_repo(
    squad: object | None, repo: str, default_provider: object
) -> object:
    """Resolve o issue provider efetivo para um repo (issue #311).

    Quando há uma SquadConfig, usa ``squad.issue_provider_for(repo)`` (override
    do repo > default da squad) e mapeia o nome para o adaptador via
    ``provider_for``. Sem squad, devolve o ``default_provider`` (provider raiz)
    — preservando byte-a-byte o comportamento anterior quando não há override.
    """
    if squad is None:
        return default_provider
    try:
        name = squad.issue_provider_for(repo)  # type: ignore[attr-defined]
    except Exception:
        return default_provider
    return provider_for(name)


class _LedgerStateReader:
    """StateReader concreto: deriva o State real da issue de EVIDÊNCIA externa
    (branch + PR + issue fechada), NÃO de labels de estado.

    C3 da frente estado-local (#301): o estado da esteira é 100% local (o ledger
    SQLite é a fonte de verdade do estágio). O motor ainda precisa saber quando um
    estágio de ESPERA (develop/review/qa) recebeu o sinal externo — mas esse sinal
    é FATO do repositório (a branch existe? há PR? a PR foi aprovada? a issue
    fechou?), não uma label de estado. Isso vem de ``implicit_state`` (puro, já
    testado). Assim o single-flow não lê mais nenhuma label ``flow:*``.
    """

    def __init__(self, provider: object, squad: object | None = None) -> None:
        # provider = provider raiz/default (fallback); squad = SquadConfig
        # opcional para resolver o issue_provider POR REPO (issue #311).
        self._provider = provider
        self._squad = squad

    def read_state(self, repo: str, task_key: str) -> State | None:
        # Deriva o estado de evidência externa (branch/PR/issue fechada).
        # _collect_implicit_state faz o I/O (branch check + PR + reviews) e delega
        # a lógica pura a implicit_state(). Fail-safe: erro → None (motor aguarda).
        from flow.domain.gates import WorkItem
        from flow.scan.scanner import ScanResult

        # Resolve o provider POR REPO: se há squad, usa issue_provider_for(repo)
        # (override do repo > default da squad); senão, o provider default.
        provider = _resolve_provider_for_repo(self._squad, repo, self._provider)

        # O reader recebe só repo + task_key; monta um ScanResult mínimo para
        # reusar _collect_implicit_state (que espera result.item.key).
        stub = ScanResult(
            item=WorkItem(key=task_key, title="", labels=frozenset()),
            current_state=None,
            modifiers=frozenset(),
            dispatch_candidate=False,
            spec_valid=None,
            changed=False,
            reason="single-flow read_state",
        )
        implicit = _collect_implicit_state(stub, provider, repo)
        if implicit is None:
            return None
        mapping = _build_implicit_to_explicit_map()
        return mapping.get(implicit)  # type: ignore[arg-type]


class _LedgerDispatcher:
    """Dispatcher concreto: mapeia o State ativo para o _dispatch_* real."""

    def __init__(
        self, ctx: object, cfg: dict, provider: object, squad: object | None = None
    ) -> None:
        self._ctx = ctx
        self._cfg = cfg
        # provider = provider raiz/default (fallback); squad = SquadConfig
        # opcional para resolver o issue_provider POR REPO (issue #311).
        self._provider = provider
        self._squad = squad

    def _enrich_issue(self, repo: str, issue: dict) -> dict | None:
        """Completa o ``issue`` do motor com ``title`` (e demais campos) via
        ``get_work_item``.

        O motor (``ledger_tick``) monta o dict só com ``number`` e ``key`` — ele
        não conhece o título da issue. Os prompts de dispatch (`_spec_stage_prompt`)
        montam o título da sessão com ``issue['title']``; sem esse campo o dispatch
        estourava ``KeyError: 'title'`` (Gap B), o que criava o backstop lock e
        abortava sem NUNCA subir a sessão sidebar.

        Busca o work_item real (1 chamada de rede, só no dispatch — não no scan) e
        devolve o ``issue`` mesclado. Retorna ``None`` se a busca falhar, para que
        ``dispatch_stage`` reporte abort (``False``) e o tick NÃO avance no vazio
        (mesma disciplina do Gap A)."""
        key = issue.get("key") or issue.get("number")
        provider = _resolve_provider_for_repo(self._squad, repo, self._provider)
        try:
            item = provider.get_work_item(repo, str(key))  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning(
                "single-flow: get_work_item falhou ao enriquecer o issue %s (%s): %s — dispatch abortado",
                key, repo, exc,
            )
            return None
        # Preserva number/key do motor; acrescenta title e o que mais vier do provider.
        merged = {**item, **issue}
        if "title" not in merged or not merged.get("title"):
            merged["title"] = item.get("title", "")
        return merged

    def dispatch_stage(self, repo: str, issue: dict, stage: State) -> object:
        if stage not in (State.BRIEFING, State.PLANNING_SPECS):
            logger.info("single-flow: estágio %s não é ativo — sem disparo", stage.value)
            return False
        enriched = self._enrich_issue(repo, issue)
        if enriched is None:
            return False
        if stage is State.BRIEFING:
            ok = _dispatch_briefing(self._ctx, repo, enriched, self._cfg)
        else:
            ok = _dispatch_planning(self._ctx, repo, enriched, self._cfg)
        if not ok:
            return False
        # Sucesso: devolve um marcador de sessão (slot) para o motor gravar como
        # stage_session e não redisparar este estágio (Gap C).
        number = issue.get("number") or issue.get("key")
        return f"{stage.value}:{repo.split('/')[-1]}-{number}"


def _resolve_squad_id(cfg: dict) -> str:
    """Resolve o id canônico da squad para nomear o RunLedger.

    O id real (ex: ``kirocrew-flow``) vive DENTRO do yaml apontado por
    ``squad_config`` (ou de um arquivo em ``squads_dir``) — a config raiz não
    tem a chave ``squad_id``. O modo parallel resolve isso via
    ``load_squad(...).id``; aqui replicamos a MESMA ordem de seleção de
    ``_load_single_flow_squad`` para que o single-flow abra o MESMO banco
    (``run_ledger_kirocrew-flow.db``), e não o ``run_ledger_default.db``.

    Ordem: (1) squads_dir → squad selecionada → ``squad.id``; (2) yaml de
    ``squad_config`` → ``squad.id``; (3) chave ``squad_id`` da config raiz;
    (4) ``"default"``.
    """
    from flow.config.squad import (
        SquadConfigError,
        load_squad,
        load_squads_dir,
    )

    repos: list[str] = cfg.get("repos") or []

    # (1) squads_dir: seleciona a squad que casa com cfg['repos'].
    squads_dir = cfg.get("squads_dir")
    if squads_dir and os.path.isdir(squads_dir):
        try:
            squad = _select_squad_for_repos(load_squads_dir(squads_dir), repos)
            if squad is not None:
                return squad.id
        except SquadConfigError as exc:
            logger.warning(
                "single-flow: squads_dir inválido (%s) — caindo para squad_config/squad_id",
                exc,
            )

    # (2) squad_config: arquivo único.
    squad_file = cfg.get("squad_config")
    if squad_file and os.path.exists(squad_file):
        try:
            return load_squad(squad_file).id
        except SquadConfigError as exc:
            logger.warning(
                "single-flow: squad_config inválido (%s) — caindo para squad_id da config",
                exc,
            )
    return cfg.get("squad_id", "default")


_SPEC_STAGES: frozenset[str] = frozenset({
    "flow:briefing",
    "flow:planning-specs",
})
"""Estágios que criam sessão sidebar (sem worktree/PR).

Para estes estágios ``_issue_has_active_session`` é cego (não há worktree nem
PR para observar). O sinal correto é checar se o SLOT ainda existe no gateway
e está ``running: true`` via GET /api/chat/slots.
"""


def _spec_slot_is_running(slot_name: str, port: int, secret: str) -> bool:
    """Verifica se um slot de spec-stage ainda está rodando no gateway.

    Faz GET /api/chat/slots e procura o slot pelo nome exato. Retorna
    ``True`` se o slot existe e tem ``running: true``.

    Fail-safe: qualquer erro de rede retorna ``False`` (o motor assume que a
    sessão terminou e avança — comportamento conservador igual ao anterior).
    """
    import json as _json
    import urllib.request as _u

    try:
        req = _u.Request(
            f"http://localhost:{port}/api/chat/slots",
            headers={"X-Internal-Secret": secret},
        )
        with _u.urlopen(req, timeout=5) as resp:
            slots: list[dict] = _json.loads(resp.read())
        for s in slots:
            if s.get("key") == slot_name:
                return bool(s.get("running", False))
        # Slot não encontrado: sessão já foi limpa pelo gateway.
        return False
    except Exception as exc:
        logger.warning(
            "single-flow: falha ao checar slot %r no gateway (assume não-viva): %s",
            slot_name, exc,
        )
        return False


def _load_provider_env(provider_name: str) -> None:
    """Carrega variáveis de ambiente do provider de um arquivo .env opcional.

    Procura por <crons_dir>/<provider>.env e seta as vars no processo atual
    (sem sobrescrever vars já definidas). Permite configurar credenciais do
    Jira (JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN) e outros providers fora
    do código, num arquivo chmod 600.

    Se o arquivo não existir, retorna silenciosamente.
    """
    env_file = os.path.join(os.path.dirname(__file__), f"{provider_name}.env")
    if not os.path.exists(env_file):
        return
    try:
        with open(env_file) as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = val.strip()
    except Exception as exc:
        logger.warning("single-flow: falha ao carregar %s: %s", env_file, exc)


def _single_flow_tick(ctx: object) -> str:
    """Um ciclo do motor ledger-driven. Retorna o diagnóstico do tick."""
    from flow.domain.run_ledger import SqliteRunLedger
    from flow.engine.ledger_tick import tick

    _check_installed_version(ctx)
    cfg = _load_config()
    squad_id = _resolve_squad_id(cfg)
    issue_provider_name = cfg.get("issue_provider", "github")

    # Carrega vars de ambiente do provider (ex: jira.env com JIRA_BASE_URL etc.)
    _load_provider_env(issue_provider_name)

    provider = provider_for(issue_provider_name)
    squad = _load_single_flow_squad(cfg)
    ledger = SqliteRunLedger(squad_id)
    try:
        reader = _LedgerStateReader(provider, squad=squad)
        dispatcher = _LedgerDispatcher(ctx, cfg, provider, squad=squad)

        # Gap C: calcula se a sessão do estágio atual ainda está viva. Sem isso
        # o tick passava stage_running=False sempre e redisparava a cada 1min,
        # criando um enxame de sessões. Só checamos quando o ledger já registrou
        # uma stage_session (senão não há sessão a "prender").
        #
        # Item A: dois caminhos distintos por tipo de estágio —
        #   spec-stages (briefing/planning-specs): criam sessão SIDEBAR sem
        #     worktree nem PR → _issue_has_active_session é cego para eles.
        #     Verificar pelo slot no gateway via GET /api/chat/slots.
        #   develop/review/qa: criam worktree + PR → _issue_has_active_session
        #     usa esses sinais (caminho anterior preservado).
        stage_running = False
        active = ledger.active()
        if active is not None and active.stage_session:
            if active.current_stage in _SPEC_STAGES:
                # Spec-stage: o stage_session gravado É o slot name.
                # Checar diretamente no gateway se o slot ainda está running.
                port = getattr(ctx, "_port", None)
                _sock_pattern = os.path.expanduser("~/.kiro/crew/dashboard-*.sock")
                _socks = glob.glob(_sock_pattern)
                if _socks:
                    import re as _re
                    _m = _re.search(r"dashboard-(\d+)\.sock", _socks[0])
                    if _m:
                        port = int(_m.group(1))
                if not port:
                    port = 5478
                secret = ""
                _gw_secret = os.path.expanduser(
                    f"~/.kiro/crew/run/gateway-{port}.secret"
                )
                if os.path.exists(_gw_secret):
                    with open(_gw_secret) as _f:
                        secret = _f.read().strip()
                if not secret:
                    _loc = os.path.expanduser("~/.kiro/crew/.local_secret")
                    if os.path.exists(_loc):
                        with open(_loc) as _f:
                            secret = _f.read().strip()
                if not secret:
                    secret = getattr(ctx, "_secret", "")
                stage_running = _spec_slot_is_running(
                    active.stage_session, port, secret
                )
            else:
                # Develop/review/qa: usa worktree + PR + backstop lock (caminho anterior).
                dev_root = cfg.get("dev_root") or os.path.expanduser(
                    "~/.kiro/crew/kirocrew-flow/worktrees"
                )
                number = _task_number_from_key(active.task_key)
                if number is not None:
                    try:
                        stage_running = _issue_has_active_session(
                            active.repo, number, dev_root
                        )
                    except Exception as exc:
                        logger.warning(
                            "single-flow: falha ao checar sessão ativa de %s (assume não-viva): %s",
                            active.task_key, exc,
                        )
                        stage_running = False

        result = tick(ledger, dispatcher, reader, stage_running=stage_running)
        logger.info("deployment[single-flow]: tick → %s", result)
        return result
    finally:
        ledger.close()


def _task_number_from_key(task_key: str) -> int | None:
    """Extrai o número da issue de um task_key ('owner/repo#42' → 42)."""
    import re

    m = re.search(r"[#/](\d+)$", task_key or "")
    return int(m.group(1)) if m else None


# ── Camada de entrada do single-flow (frente 8) ──────────────────────────────
#
# O motor (`_single_flow_tick`) só avança uma task que JÁ esteja no ledger. Sem
# uma porta de entrada, `ledger.active()` é sempre None e o tick fica idle
# eterno. `claim_single_flow` é essa porta: registra a task no ledger no estado
# de entrada (flow:briefing), a partir daí o tick a empurra estágio a estágio.


def _parse_claim_message(message: str) -> tuple[str, str | int]:
    """Extrai ``(projeto/repo, chave_ou_número)`` da mensagem de claim.

    Formatos aceitos:

    GitHub:
        "owner/repo#42"
        "owner/repo 42"
        "owner/repo/issues/42"
        "https://github.com/owner/repo/issues/42"

    Jira:
        "VGAT-1009"          → ("VGAT", "VGAT-1009")
        "PROJ-42"            → ("PROJ", "PROJ-42")

    Levanta ``ValueError`` quando não consegue extrair projeto + chave.
    """
    import re

    text = (message or "").strip()
    if not text:
        raise ValueError("mensagem de claim vazia — esperado 'owner/repo#42' ou 'VGAT-1009'")

    # Jira key: PROJETO-NUMERO (ex: VGAT-1009, VSUS-42)
    m = re.match(r"^([A-Z][A-Z0-9]+)-(\d+)$", text)
    if m:
        project = m.group(1)
        key = text  # mantém a chave completa (ex: "VGAT-1009")
        return project, key

    # URL completa do GitHub
    m = re.match(r"https?://github\.com/([^/]+/[^/]+)/issues/(\d+)", text)
    if m:
        return m.group(1), int(m.group(2))

    # owner/repo#42  |  owner/repo/issues/42
    m = re.match(r"([^\s/]+/[^\s/#]+)(?:/issues)?#?/?(\d+)$", text)
    if m:
        return m.group(1), int(m.group(2))

    # owner/repo 42  (separado por espaço)
    m = re.match(r"([^\s/]+/[^\s]+)\s+#?(\d+)$", text)
    if m:
        return m.group(1), int(m.group(2))

    raise ValueError(
        f"não consegui extrair projeto + chave de {text!r} — "
        "esperado 'owner/repo#42' (GitHub) ou 'VGAT-1009' (Jira)"
    )


def claim_single_flow(ctx: object) -> str:
    """Entrypoint de ENTRADA do single-flow: injeta uma task no RunLedger.

    A task é lida de ``ctx.message`` e registrada no ledger no estado de entrada
    (``flow:briefing``). É idempotente: reclamar a mesma ``task_key`` já ativa
    retorna sem erro. Só uma task fica ativa por vez.

    Formatos aceitos em ``ctx.message``:

    GitHub:
        "owner/repo#42"          → task_key="owner/repo#42", repo="owner/repo"
        "https://github.com/owner/repo/issues/42"

    Jira:
        "VGAT-1009"              → task_key="VGAT-1009", repo="VGAT"

    O ``repo`` gravado no ledger é o que ``provider.get_work_item(repo, key)``
    espera: para GitHub é "owner/repo", para Jira é o projeto ("VGAT").
    """
    from flow.domain.run_ledger import SqliteRunLedger

    message = str(getattr(ctx, "message", "") or "")
    try:
        project_or_repo, key_or_number = _parse_claim_message(message)
    except ValueError as exc:
        msg = f"single-flow[claim]: {exc}"
        logger.error(msg)
        _notify(ctx, msg)
        return "error:bad-message"

    # Para Jira: key_or_number é string (ex: "VGAT-1009"), repo é o projeto
    # Para GitHub: key_or_number é int (ex: 42), task_key = "owner/repo#42"
    if isinstance(key_or_number, str):
        # Jira: task_key = a própria chave Jira ("VGAT-1009"), repo = projeto
        task_key = key_or_number
        repo = project_or_repo
    else:
        task_key = f"{project_or_repo}#{key_or_number}"
        repo = project_or_repo

    entry_stage = State.BRIEFING.value

    cfg = _load_config()
    squad_id = _resolve_squad_id(cfg)
    ledger = SqliteRunLedger(squad_id)
    try:
        claimed = ledger.claim(task_key, repo, entry_stage)
    finally:
        ledger.close()

    if claimed:
        msg = f"single-flow[claim]: {task_key} registrada em {entry_stage}"
        logger.info(msg)
        _notify(ctx, msg)
        return f"claimed:{task_key}"

    # claim retornou False: já há OUTRA task ativa (limite de 1 por vez)
    msg = (
        f"single-flow[claim]: {task_key} NÃO registrada — já há outra task "
        "ativa no ledger (limite: 1 por vez)"
    )
    logger.warning(msg)
    _notify(ctx, msg)
    return f"rejected:{task_key}"


def _notify(ctx: object, msg: str) -> None:
    """Notifica via ctx.notify() quando disponível, silencioso caso contrário."""
    if ctx is None:
        return
    try:
        ctx.notify(msg)  # type: ignore[attr-defined]
    except Exception as exc:
        logger.debug("single-flow: falha ao notificar: %s", exc)


# ── Stub de compatibilidade — re-exporta entrypoints de deployment/flow/ ─────
#
# Os crons novos apontam para deployment/flow/<modulo>.py:run.
# Os crons existentes (run_dev, run_reviewer, run_merge, run_conflito) continuam
# funcionando via as funções definidas acima — não há quebra de compatibilidade.
#
# Importações dos módulos flow/ disponíveis para uso direto quando instalados:
#
#   from deployment.flow.dev      import run as run_dev_flow
#   from deployment.flow.reviewer import run as run_reviewer_flow
#   from deployment.flow.merge    import run as run_merge_flow
#   from deployment.flow.conflict import run as run_conflict_flow
#   from deployment.flow.rework   import run as run_rework_flow
#
# Novos scripts de cron (após install-cron.sh):
#   script="~/.kiro/crew/crons/deployment/flow/dev.py:run"
#   script="~/.kiro/crew/crons/deployment/flow/reviewer.py:run"
#   script="~/.kiro/crew/crons/deployment/flow/merge.py:run"
#   script="~/.kiro/crew/crons/deployment/flow/conflict.py:run"
#   script="~/.kiro/crew/crons/deployment/flow/rework.py:run"
