"""kirocrew-deployment — vigia de issues `aguardando-desenvolvimento` + motor de execução one-shot.

Cron de SCRIPT do Kiro Crew (sem LLM, zero token no polling). Bate no GitHub via
`gh` em cada repo configurado, e para cada issue nova com label `aguardando-desenvolvimento` (sem
`crew: in progress`):

  - auto_dispatch=false (Fase 1): só AVISA (ctx.notify) — você aciona manual.
  - auto_dispatch=true  (Fase 2): dispara uma SESSÃO ONE-SHOT (POST /api/chat via
    loopback interno) que implementa a issue e encerra — sem loop, sem watchdog.

Depende do Kiro Crew rodando (usa o loopback interno + o formato de cron de
script). NÃO é standalone. Veja o README.

Registro (uma vez):
    cron_add(name="esteira", script="~/.kiro/crew/crons/deployment.py:run", every=600)

Config: ~/.kiro/crew/crons/deployment.config.yaml (copie de config.example.yaml).
"""
import glob
import json
import os
import subprocess

# ── Labels da esteira (taxonomia pt-BR, sem prefixo crew:) ─────────────────
LABEL_READY = "aguardando-desenvolvimento"   # gatilho: a esteira pega
LABEL_IN_PROGRESS = "em-desenvolvimento"     # sessão implementando
LABEL_TESTING = "em-teste"                    # validando (testes/QA) antes do PR
LABEL_NEEDS_HUMAN = "acao-necessaria"        # travou, precisa de decisão
LABEL_HOLD = "segurar"                        # não fazer auto-merge
LABEL_BLOCKED = "bloqueado"                   # travado por dependência
LABEL_REVIEW = "aguardando-code-review"       # PR aberto, esperando revisão

# ── carregamento de config ────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG_CANDIDATES = [
    os.path.join(_HERE, "deployment.config.yaml"),
    os.path.expanduser("~/.kiro/crew/crons/deployment.config.yaml"),
]


def _load_config():
    """Lê a config YAML (parser mínimo, sem dependência externa)."""
    path = next((p for p in _CONFIG_CANDIDATES if os.path.exists(p)), None)
    if not path:
        raise RuntimeError(
            "esteira: config não encontrada. Copie config.example.yaml para "
            "deployment.config.yaml ao lado do script."
        )
    try:
        import yaml  # type: ignore
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return _mini_yaml(path)


def _mini_yaml(path):
    """Parser YAML minimalista: chaves escalares + listas simples (- item)."""
    cfg, cur_list_key = {}, None
    with open(path) as f:
        for raw in f:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            if line.lstrip().startswith("- ") and cur_list_key:
                cfg[cur_list_key].append(line.lstrip()[2:].strip().strip('"\''))
                continue
            if ":" in line and not line.startswith(" "):
                key, _, val = line.partition(":")
                key, val = key.strip(), val.strip().strip('"\'')
                if val == "":
                    cfg[key], cur_list_key = [], key
                else:
                    cur_list_key = None
                    if val.lower() in ("true", "false"):
                        cfg[key] = val.lower() == "true"
                    elif val.isdigit():
                        cfg[key] = int(val)
                    else:
                        cfg[key] = val
    return cfg


def _state_dir():
    d = os.path.join(_HERE, "state")
    os.makedirs(d, exist_ok=True)
    return d


def _state_file(repo):
    return os.path.join(_state_dir(), "ready-" + repo.replace("/", "__") + ".json")


def _ready_issues(repo):
    out = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--label", LABEL_READY, "--json", "number,title,url,labels", "--limit", "30"],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        return None
    try:
        issues = json.loads(out.stdout)
    except Exception:
        return None
    filtered = []
    for i in issues:
        names = {lb.get("name", "") for lb in i.get("labels", [])}
        if LABEL_IN_PROGRESS in names:
            continue
        i["_has_hold"] = LABEL_HOLD in names
        filtered.append(i)
    return filtered


def _load_seen(repo):
    try:
        with open(_state_file(repo)) as f:
            return set(json.load(f).get("seen", []))
    except Exception:
        return set()


def _save_seen(repo, seen):
    with open(_state_file(repo), "w") as f:
        json.dump({"seen": sorted(seen)}, f)


def _sessdir():
    return os.path.expanduser("~/.kiro/crew/sessions")


def _active_sessions():
    return len(glob.glob(os.path.join(_sessdir(), "dashboard_esteira-*.jsonl.lock")))


def _repo_has_active(repo):
    short = repo.split("/")[-1]
    return bool(glob.glob(os.path.join(_sessdir(), f"dashboard_esteira-{short}-*.jsonl.lock")))


def _dispatch_prompt(repo, issue, cfg):
    short = repo.split("/")[-1]
    vault = cfg.get("vault_root") or ""
    dev_root = cfg.get("dev_root") or os.path.expanduser("~/dev")
    chat_id = cfg.get("notify_chat_id") or ""
    vault_step = ""
    if vault:
        vault_step = (
            f"   - VAULT: edite `{vault}/Projetos/{short}/backlog.md` refletindo a issue "
            f"resolvida e sincronize com `sh {vault}/.sync.sh \"<msg>\"` (NUNCA `git push` "
            "literal). Se a pasta não existir, pule sem erro.\n"
        )
    notify_step = (
        f"avise via voice_maybe (chat_id {chat_id}, intent auto) com TL;DR, "
        if chat_id else "reporte o resultado, "
    )
    return (
        "------------ AGENT HEADER ----------------\n"
        f"REPO: {repo}\n"
        f"ISSUE: #{issue['number']} — {issue['title']}\n"
        f"URL: {issue['url']}\n"
        f"SESSION TITLE: {short} #{issue['number']}: {issue['title']}\n"
        "------------ CONTEXT TASK ----------------\n"
        "Você é um agente de implementação ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.\n\n"
        "FLUXO (execute UMA vez, do início ao fim, e PARE):\n"
        "0. TÍTULO: como PRIMEIRA ação, defina o título da sessão = `SESSION TITLE` (se a tool "
        "de título/mover-sessão existir; senão siga sem travar — é só acabamento).\n"
        "1. Leia a issue (gh issue view) e a doc do repo (.kiro/steering/, README).\n"
        "2. ESCOPO: se a issue exige decisão de design não-tomada ou é vaga, NÃO implemente — "
        f"comente, marque `{LABEL_NEEDS_HUMAN}`, avise e ENCERRE.\n"
        f"3. Marque `{LABEL_IN_PROGRESS}`. NÃO faça `git clone`. Use o clone em `{dev_root}/{short}` "
        f"como base e crie um WORKTREE ISOLADO:\n"
        f"   `cd {dev_root}/{short} && git fetch origin && git worktree add -b "
        f"feat/issue-{issue['number']} {dev_root}/.esteira-worktrees/{short}-{issue['number']} "
        "origin/main`\n"
        "   Trabalhe DENTRO do worktree; remova-o ao fim (`git worktree remove --force ...`). "
        "NUNCA toque em outros worktrees/branches.\n"
        "4. Implemente EXATAMENTE o escopo — nada além.\n"
        f"5. Troque a label da issue para `{LABEL_TESTING}` e valide localmente "
        "(build/testes/QA). Se falhar e não conseguir corrigir no escopo, pare em "
        f"`{LABEL_NEEDS_HUMAN}`.\n"
        f"6. Abra PR com 'Closes #{issue['number']}'. Se a issue tem `{LABEL_HOLD}`: NÃO "
        f"mergeie — troque a label da issue para `{LABEL_REVIEW}` e deixe o PR pro humano. "
        "Senão, mergeie via squash quando verde (auto-merge).\n"
        f"7. Ao terminar: {notify_step}LIMPE as labels de fluxo "
        f"(`{LABEL_READY}`/`{LABEL_IN_PROGRESS}`/`{LABEL_TESTING}`) da issue, e ENCERRE.\n"
        f"{vault_step}\n"
        "REGRAS CRÍTICAS:\n"
        "- UMA passada. Terminou, acabou. NÃO fique verificando, NÃO entre em loop.\n"
        f"- Se algo bloquear, marque `{LABEL_BLOCKED}`/`{LABEL_NEEDS_HUMAN}`, avise, e pare.\n"
        "------------------------------------------"
    )


def _dispatch(ctx, repo, issue, cfg):
    """Fire-and-forget POST /api/chat (loopback interno, roda in-process, sem sandbox)."""
    import urllib.request as _u
    slot = f"esteira-{repo.split('/')[-1]}-{issue['number']}"
    body = json.dumps({
        "message": _dispatch_prompt(repo, issue, cfg),
        "agent": cfg.get("agent") or "kirocrew",
        "slot": slot,
        "memory_mode": "temporary",
    }).encode()
    req = _u.Request(
        f"http://localhost:{ctx._port}/api/chat",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Secret": ctx._secret,
            "X-Session-Key": f"cron:{ctx.job.id}",
        },
        method="POST",
    )
    try:
        from kiro_crew.loopback_http import loopback_urlopen
        with loopback_urlopen(req, timeout=3) as resp:
            resp.read(1)  # confirma enfileiramento; timeout do SSE é esperado
    except Exception:
        pass


def run(ctx):
    cfg = _load_config()
    repos = cfg.get("repos") or []
    auto = bool(cfg.get("auto_dispatch", False))
    max_conc = int(cfg.get("max_concurrent", 2))
    one_per_repo = bool(cfg.get("one_per_repo", True))
    chat_id = cfg.get("notify_chat_id") or ""

    disparadas, adiadas, achados = [], [], []
    houve_erro = False
    vagas = (max_conc - _active_sessions()) if auto else 0

    for repo in repos:
        issues = _ready_issues(repo)
        if issues is None:
            houve_erro = True
            continue
        seen = _load_seen(repo)
        current = {i["number"] for i in issues}
        novas = sorted((i for i in issues if i["number"] not in seen),
                       key=lambda i: i["number"])
        base_seen = set(seen) & current

        if not auto:
            _save_seen(repo, current)
            if novas:
                achados.append((repo, novas))
            continue

        if not novas:
            _save_seen(repo, base_seen)
            continue

        if one_per_repo and _repo_has_active(repo):
            adiadas.extend((repo, i) for i in novas)
            _save_seen(repo, base_seen)
            continue

        repo_done = False
        for issue in novas:
            if repo_done or vagas <= 0:
                adiadas.append((repo, issue))
                continue
            try:
                _dispatch(ctx, repo, issue, cfg)
                disparadas.append((repo, issue))
                base_seen.add(issue["number"])
                vagas -= 1
                if one_per_repo:
                    repo_done = True
            except Exception as e:
                achados.append((repo, [dict(issue, _err=str(e))]))
        _save_seen(repo, base_seen)

    if not achados and not disparadas and not adiadas:
        if houve_erro:
            from kiro_crew.cron import Skip
            raise Skip()
        return

    vm = f" (voice_maybe chat_id {chat_id}, intent auto)" if chat_id else ""
    if auto and disparadas:
        linhas = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in disparadas)
        extra = ""
        if adiadas:
            fila = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in adiadas)
            extra = f"\n\nNA FILA (limite/1-por-repo, disparam depois):\n{fila}"
        ctx.notify(
            f"kirocrew-deployment: disparei sessão(ões) one-shot pra issue(s) `ready`. Avise o dono{vm} "
            f"que o disparo aconteceu.\n{linhas}{extra}"
        )
    elif auto and adiadas:
        fila = "\n".join(f"  - {r}#{i['number']}: {i['title']}" for r, i in adiadas)
        ctx.notify(f"kirocrew-deployment: {len(adiadas)} issue(s) `ready` na fila (limite cheio).{vm}\n{fila}")
    elif achados:
        blocos = [f"{r}:\n" + "\n".join(f"  - #{i['number']}: {i['title']} ({i['url']})"
                  for i in ns) for r, ns in achados]
        ctx.notify(
            f"kirocrew-deployment (Fase 1): há issue(s) `ready` esperando. Avise o dono{vm} com o TL;DR.\n"
            + "\n".join(blocos)
        )
