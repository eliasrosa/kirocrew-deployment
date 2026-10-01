---
inclusion: always
name: arquitetura-codigo
description: Arquitetura hexagonal, stack, convenções de código e qualidade do KiroCrew Flow. Ler antes de tocar qualquer arquivo Python do projeto.
---

# Arquitetura do KiroCrew Flow

## Stack

| Componente | Tecnologia | Observação |
|---|---|---|
| Linguagem | **Python 3.12** | |
| Servidor web | **aiohttp** | NÃO usar FastAPI, Flask, Starlette ou Pydantic |
| Testes | **pytest** + **pytest-cov** | 827 testes, 75% de cobertura mínima |
| Linting | **ruff** | zero warnings permitidos |
| Type check | **mypy** | `--ignore-missing-imports` no CI |

## Estrutura completa (Fase 1 concluída)

```
flow/
├── domain/                     ← NÚCLEO: sem I/O, testável sem mock
│   ├── state.py                — State, Modifier, is_dispatchable(), can_transition()
│   └── gates.py                — Result, WorkItem, Squad, can_leave_spec(), triage_hotfix()...
├── ports/
│   └── issue_provider.py       — IssueProvider (Protocol), provider_for(), PROVIDERS
├── adapters/
│   ├── github_transport.py     — fachada patchável sobre gh CLI
│   ├── github_normalization.py — payload GitHub → contrato canônico
│   ├── github_client.py        — orquestração; satisfaz IssueProvider
│   ├── jira_transport.py       — REST HTTP via stdlib
│   ├── jira_normalization.py   — payload Jira → contrato canônico
│   ├── jira_client.py          — orquestração; satisfaz IssueProvider
│   └── scm/                    — adaptadores de SCM (PR, CI, merge)
│       ├── factory.py          — ScmFactory: instancia GitHub ou Azure DevOps por URL
│       ├── github.py           — GitHubTransport (gh CLI)
│       └── azure_devops.py     — AzureDevOpsTransport (REST API + AZURE_DEVOPS_PAT)
├── scan/
│   ├── cache.py                — SQLite: hash de labels por issue (zero token) + running_since
│   └── scanner.py              — scan_candidates(): filtra candidatos a dispatch
├── executor/
│   └── executor.py             — decide(): decide a ação por template sem I/O
├── audit/
│   └── state_comment.py        — render/parse do <!-- KIRO-FLOW-STATE -->
├── prompts/                    ← templates MD editáveis por estágio (Fase 2)
│   ├── develop_waiting.md      — prompt do dev (implementa + abre PR)
│   ├── review_waiting.md       — prompt do reviewer (code review)
│   ├── merge_conflict.md       — prompt de resolução de conflito de merge
│   └── loader.py               — render_prompt(): carrega, interpola e valida
└── config/
    ├── squad.py                — SquadConfig, RoutingRule, RepoConfig, load_squad()
    └── workflow.py             — WorkflowTemplate, get_template(), 4 templates fixos

deployment/
├── deployment.py               — driving adapter (run_single_flow + claim_single_flow)
├── deployment.config.yaml      — config de paths, flags globais e concorrência
└── flow/                       — módulos do cron single-flow
    ├── single_flow.py          — entrypoint do cron flow-single (ledger-driven, O(1))
    └── scripts/                — scripts helper de integração com o engine de workflow
        ├── check_dispatchable.py
        ├── route_state.py
        ├── advance_state.py
        ├── check_pr_state.py
        ├── check_qa_state.py
        └── merge_pr.py

> **Legado removido (PR #319, out/2026):** os arquivos `dev.py`, `reviewer.py`,
> `review_approved.py`, `rework.py`, `merge_conflict.py`, `qa_waiting.py`,
> `qa_approved.py`, `qa_refused.py` e `watch_issue.py` foram removidos junto com
> as funções `run()`, `_run_stage()` e `scan_candidates()` do `deployment.py`.

flow/tests/
├── test_domain_boundary.py     — garante domain/ isolado (NUNCA viola)
├── test_domain_state.py
├── test_domain_gates.py
├── test_ports_issue_provider.py
├── test_adapter_github.py
├── test_adapter_jira.py
├── test_provider_parity.py     — garante que os dois adapters expõem a mesma superfície
├── test_scan_cache.py
├── test_scan_scanner.py
├── test_executor.py
├── test_audit_state_comment.py
├── test_config_squad_workflow.py
└── test_deployment_integration.py

resources/mermaid/              ← fonte .mmd dos 4 fluxos (a verdade)
docs/diagramas/                 ← PNG derivado + README com contexto
squads/
└── example.yaml                — schema documentado
workflows/
├── feature-flow.yaml
├── bug-flow.yaml
├── hotfix-flow.yaml
└── debt-flow.yaml
```

## Regra de isolamento do domínio (INVIOLÁVEL)

O teste `test_domain_boundary.py` quebra o CI se qualquer arquivo em `flow/domain/`
importar:
- `flow.adapters`
- `aiohttp`, `subprocess`, `socket`, `requests`, `httpx`
- qualquer outra biblioteca de I/O

**Por quê:** o domínio isolado é testável sem mock nenhum — é o argumento central da
arquitetura. Se o teste de fronteira quebrar, pare tudo e conserte antes de avançar.

## Adapters são MÓDULOS, não classes

O dispatch é um dict de módulos:
```python
_PROVIDERS = {"github": github_client, "jira": jira_client}
provider_for("jira")  # → o módulo jira_client
```

Um módulo **não pode** ser verificado estaticamente contra um Protocol. O gate que
garante paridade é `test_provider_parity.py`. Se você adicionar um terceiro adapter,
registre-o também na tabela `CLIENTS` do teste — senão o CI passa sem verificar.

## Motor: single-flow ledger-driven (atual)

O motor atual é **ledger-driven** (frentes 6–8, set/2026). O estado da task vive
num SQLite local, não nas labels da issue.

```
deployment.run_single_flow(ctx)
    │
    ├─ ledger.active()                       ← 1 query SQLite (O(1))
    │   sem task ativa → claim_single_flow() ← busca próxima issue
    │
    ├─ provider.get_work_item(issue_key)     ← 1 chamada de rede
    │
    └─ ledger_tick.tick(ledger, work_item, dispatcher)
           │
           ├─ estado ATIVO (BRIEFING/PLANNING_SPECS)
           │   → dispatcher.dispatch_stage()  ← abre sessão one-shot
           │   → ledger.advance(próximo_estado)
           │
           └─ estado ESPERA (DEVELOP/REVIEW/QA)
               → observa sinal externo (PR aberto, review aprovado)
               → ledger.advance(próximo_estado) quando sinal chega
```

**Protocolo de encerramento:** o agente one-shot publica `WORKFLOW_EXIT: {"exit_status": "done"}`
na última mensagem. O motor lê o JSONL da sessão de trás para frente e avança.

**Dispatch:** 2 POSTs ao gateway local — `/api/chat/slots` (cria slot, obtém `tab_id`)
+ `/api/chat` (injeta instrução). Secret lido de `~/.kiro/crew/run/gateway-{port}.secret`.

> **Legado (label-driven, até set/2026):** antes o fluxo era guiado por `scan_candidates()`
> + `executor.decide()` + labels `flow:*` + `KIRO-FLOW-STATE` comment. Esse código foi
> removido na PR #319. Ver `docs/ARCHITECTURE.md` para o histórico.

## Fluxo de uma issue pela arquitetura (legado — referência histórica)

```
squads/*.yaml  →  SquadConfig.resolve_workflow()  →  template (feature/bug/hotfix/debt)
                                    ↓
scan_candidates(squad_config, provider, conn)  ←  zero token, SQLite cache
                                    ↓ ScanResult
executor.decide(scan_result, state_comment)    ←  puro Python, sem I/O
                                    ↓ ExecutorDecision
deployment.py  →  set_labels() + upsert_state_comment() + _dispatch()
```

## Convenções de código

### Enums de label

Use `StrEnum` (não `str, Enum`):
```python
class State(StrEnum):
    DEVELOP_WAITING = "flow:develop-waiting"
```

### Dataclasses

Sempre `frozen=True, slots=True`:
```python
@dataclass(frozen=True, slots=True)
class WorkItem:
    key: str
    title: str
```

### Injeção de dependência

Resultados de decisões externas chegam como parâmetros, nunca buscados:
```python
# CORRETO
def can_start_debt(item, has_tl_approval: bool) -> Result: ...
```

### Imports nos adapters

Todos os imports no topo do arquivo. Import local dentro de função
só em caso de circular import genuíno (documentar o motivo).

### `with` múltiplos

```python
# CORRETO
with mock.patch.object(transport, "fn"), pytest.raises(Error):
    ...
```

## Adicionar um novo provedor

1. Criar `flow/adapters/<nome>_transport.py`, `_normalization.py` e `_client.py`
2. Adicionar `"<nome>"` em `PROVIDERS` em `flow/ports/issue_provider.py`
3. Adicionar ao dispatch em `_build_dispatch()`
4. **Obrigatório:** adicionar à tabela `CLIENTS` em `test_provider_parity.py`
5. O `test_a_tabela_cobre_todos_os_providers_registrados` quebra até o passo 4

## Dependências de ambiente

### Para desenvolvimento e CI

```bash
pip install -e ".[dev]"
# Instala: pytest, pytest-cov, mypy, ruff, pyyaml
```

**PyYAML é obrigatório** para squads que usam `routing:` com formato multi-linha
no `squads/*.yaml`. Sem PyYAML, o `_mini_yaml` só suporta routing inline
(`{labels: ["crewflow:hotfix"]}`). Com PyYAML instalado, qualquer YAML válido funciona.

### Para o cron de scan (sistema)

| Dependência | Por quê | Como verificar |
|---|---|---|
| `gh` autenticado | `gh issue list`, `gh api` | `gh auth status` |
| Clone do repo | worktree de implementação | `ls /path/to/kirocrew-flow/flow/` |
| Python 3.12+ | executa o scan | `python3 --version` |
| `scripts/install-cron.sh` executado | `flow/` no sys.path | ver `~/.kiro/crew/crons/deployment.py` |

**Nunca copiar `deployment.py` manualmente** — usar `./scripts/install-cron.sh` que
aplica o patch de sys.path automaticamente. Se copiado manualmente, o cron vai
falhar com `ModuleNotFoundError: No module named 'flow'`.

## Suporte multi-SCM (GitHub + Azure DevOps)

O motor suporta dois provedores de SCM para operações de PR (abrir, checar CI, merge):

| Provedor | Transporte | Detecção automática |
|---|---|---|
| GitHub | `GitHubTransport` — `gh` CLI | URL contém `github.com/` ou formato `owner/repo` |
| Azure DevOps | `AzureDevOpsTransport` — REST API | URL contém `dev.azure.com/` |

A `ScmFactory` (`flow/adapters/scm/factory.py`) instancia o transporte correto
baseado na URL do repo, sem necessidade de configurar `scm_provider` explicitamente.

**Pré-requisito para Azure DevOps:** `AZURE_DEVOPS_PAT` no `.env` com escopo
Code (Read & Write). GitHub não exige env var — usa o `gh` CLI já autenticado.

## Separação config / secrets (`.env`)

Os secrets saíram do `deployment.config.yaml` e agora vivem em `.env` (gitignored):

| Variável | Fonte | Descrição |
|---|---|---|
| `AZURE_DEVOPS_PAT` | `.env` | PAT para REST API do Azure DevOps |
| `KIROCREW_WEBHOOK_TOKEN` | `.env` | Token Bearer do webhook do dashboard |
| `KIROCREW_WEBHOOK_SECRET` | `.env` | Signing secret HMAC-SHA256 |

Copiar `.env.example` → `.env` e preencher antes de rodar o cron. O `deployment.config.yaml`
retém apenas configurações não-sensíveis (paths, flags, concorrência).

## Config por repo (`auto_dispatch` / `auto_merge` no `squads/*.yaml`)

Cada repo pode sobrepor os flags globais de dispatch e merge:

```yaml
repos:
  - url: https://github.com/org/api-gateway2
    auto_dispatch: true
    auto_merge: false      # merge manual neste repo
  - url: https://dev.azure.com/kdop/Proj/_git/api-sub
    auto_dispatch: true
    auto_merge: true       # merge automático após approve
```

- `auto_dispatch: null` (omitido) → herda `global_auto_dispatch` do `deployment.config.yaml`
- `auto_merge: null` (omitido) → herda `workflow_params.auto_merge_on_approve`

**`auto_merge_on_approve` ainda existe** em `workflow_params` como flag global; o
campo por repo `auto_merge` tem precedência quando definido.

## Instalação do cron

**Use sempre `scripts/install-cron.sh`** em vez de copiar manualmente.

```bash
./scripts/install-cron.sh
```

O script:
1. Copia `deployment/deployment.py` para `~/.kiro/crew/crons/`
2. Aplica o patch de `sys.path` para que `flow/` seja importável do diretório do cron
3. Copia `deployment/deployment.config.yaml` se não existir

**Por que o patch é necessário:** o Kiro Crew executa o script de `~/.kiro/crew/crons/`, não do repo. O `_REPO_ROOT` calculado por `os.path.dirname(__file__)` aponta para `~/.kiro/crew/` onde `flow/` não existe. O script injeta `_FLOW_ROOT = /path/to/kirocrew-flow` antes.

Se você copiar o script manualmente sem usar `install-cron.sh`, o patch será perdido e o cron vai falhar com `ModuleNotFoundError: No module named 'flow'`.

## Rodar o CI localmente

```bash
# Lint
python3 -m ruff check flow/

# Type check
python3 -m mypy flow/ --ignore-missing-imports

# Testes + cobertura
python3 -m pytest flow/tests/ --cov=flow --cov-report=term-missing

# Tudo junto
python3 -m ruff check flow/ && python3 -m mypy flow/ --ignore-missing-imports && python3 -m pytest flow/tests/ --cov=flow --cov-fail-under=75
```

## Notas de implementação

- `list_changed_since()` nos dois adapters é ingênuo na Fase 1: varre todos os
  estados. Dívida técnica documentada — a fix está no backlog.
- Os adapters são síncronos. `aiohttp` é async-first — a migração para `async def`
  será necessária quando o cron for integrado ao servidor web. Os transports já
  estão estruturados para absorver isso.
- O executor hoje usa `_detect_template()` interno. Quando o routing da squad
  config (`SquadConfig.resolve_workflow()`) for conectado ao executor, essa função
  some — é a mesma lógica, em lugar permanente.
- `deployment.py` é o **driving adapter** da Fase 1. Quando a arquitetura for
  conectada de ponta a ponta, ele só precisará de `load_squad()`, `scan_candidates()`
  e `executor.decide()` — toda a lógica de negócio já está nos módulos.
- **Workspace isolado (Fase 2):** cada dispatch cria um worktree efêmero em
  `<dev_root>/.esteira-worktrees/<repo-short>-<issue_number>`. Use sempre
  `_worktree_path(dev_root, repo, issue_number)` para construir o caminho — é a
  fonte única de verdade, usada tanto pelo deployment quanto pelo prompt da sessão.
  O `deployment.py` limpa worktrees órfãos via `_clean_stale_worktree()` antes de
  cada dispatch. Use `max_concurrent_tasks` na config (alias de `max_concurrent`).
- **Concorrência orientada ao estado da issue:** o dispatch decide se há sessão ativa
  pelo ESTADO real — worktree presente + PR aberto + backstop lock curto (2min),
  não por timeout de arquivo de 2h. Cap primário = issues em `flow:develop-running + running`
  no scan (sem depender de locks). Sessão morta (running >40min sem sinais de vida)
  é detectada por `_is_dead_session()` e recuperada via `_recover_dead_session()` que
  remove `flow:develop-running` e notifica o TL — nunca redespacha sozinho (fail-closed).

## Prompts externalizados — `flow/prompts/`

Os prompts das sessões one-shot (dev e reviewer) vivem em arquivos MD editáveis em
`flow/prompts/`. O motor carrega e interpola esses templates via `flow/prompts/loader.py`.

### Templates disponíveis

| Arquivo | Sessão | Quando é usado |
|---|---|---|
| `develop_waiting.md` | implementação inicial | `DISPATCH_DEV` — issue em `flow:develop-waiting` |
| `review_waiting.md` | code review | `DISPATCH_REVIEWER` — issue em `flow:review-waiting` |
| `rework.md` | re-trabalho pós-review | `DISPATCH_REWORK` — issue com `flow:review-refused` |
| `merge_conflict.md` | resolução de conflito | `DISPATCH_CONFLICT_RESOLVER` — issue com `flow:merge-conflict` |

### Monitor zero-token da issue (efeito colateral do dispatch DEV)

Ao despachar uma sessão de dev com sucesso, o executor cria automaticamente um
**monitor zero-token** para a issue (`_create_issue_monitor` em `deployment.py`).
Não é uma sessão de agente — é um cron script em Python puro
(`deployment/flow/watch_issue.py:check`) que a cada 180s chama só `gh`:

- **PR abre** → notifica o usuário UMA vez (anti-spam por marcador em disco) e
  segue monitorando até o merge.
- **Issue fecha** (PR mergeada) → notifica e se auto-remove (`Done`).
- **Sem PR + sessão sem atividade > 40min** → notifica possível travamento e se
  auto-remove (`Done`).
- **Ainda implementando** → silêncio (`Skip`).

O cron é registrado idempotentemente por nome (`watch-<repo_short>-<N>`) via
`CronService.add_job_if_absent`, então um re-dispatch da mesma issue não cria um
segundo monitor. A criação é **fail-safe**: qualquer erro é logado e engolido —
nunca aborta o dispatch já concluído. O `install-cron.sh` copia `watch_issue.py`
junto dos demais módulos de `deployment/flow/`.

### Ciclo de re-trabalho (flow:review-refused)

Quando o reviewer reprova, o motor adiciona `flow:review-refused` à issue (removendo
`flow:review-waiting` e `flow:review-running`) e despacha uma sessão `rework` que:
1. Lê os pedidos de mudança nos comentários do PR
2. Aplica as correções na **mesma branch/PR** (nunca cria PR novo)
3. Commita e faz push (o novo SHA invalida `flow:review-running` automaticamente)
4. Volta a issue para `flow:review-waiting` (remove `flow:review-refused`)

Quando o reviewer aprova, o motor adiciona `flow:review-waiting-ok` (removendo
`flow:review-waiting` e `flow:review-running`). O cron `run_merge` lê `review-ok`
e executa o merge squash, sem precisar ler o state_comment.

Teto de iterações: `gates.exceeded_review_iterations()` controla o cap (default 3).
Após o teto, o executor escala para `NOTIFY_HUMAN tl` em vez de continuar despachando.
O número de iterações é registrado em `StateComment.review_iterations` (campo
`**Iterações de review:**` no comentário da issue).

### Convenção de placeholders

Os templates usam `{{nome_da_variavel}}` (duplas chaves). O loader substitui
cada placeholder pelo valor correspondente passado como keyword argument.

### Contrato de fail-closed

- **Variável faltando** → `PromptRenderError` — o dispatch é abortado, nunca envia
  prompt incompleto.
- **Template ausente/ilegível** → usa o **fallback embutido** definido em `deployment.py`
  — mesmo conteúdo que o MD versiona, nunca silencioso.

### ⚠️ Regra crítica: mudança em template/prompt EXIGE reinstalar o cron

Os templates (`flow/prompts/*.md`) são lidos em runtime pelo repo, mas o código
que passa as variáveis (`deployment/deployment.py`) é **copiado para `~/.kiro/crew/crons/`**
via `scripts/install-cron.sh`. Qualquer PR que toque `deployment/deployment.py`
**OU** `flow/prompts/*.md` DEVE ser seguido de reinstalação do cron:

```bash
./scripts/install-cron.sh
```

**Por quê:** se o template novo exigir uma variável (`{{nova_var}}`) que o código
instalado não passa, o dispatch aborta com `PromptRenderError` para TODAS as issues
do estágio afetado — até a reinstalação. Foi o que causou a issue #116.

**Detecção automática:** `deployment.py` verifica na inicialização de cada ciclo
se o script instalado bate com a versão do repo (via `deployment.version`).
Se divergir, notifica com instrução de reinstalação. Isso não substitui a reinstalação
manual — apenas avisa; o cron continua rodando com o script antigo.

**CI de paridade:** o teste `flow/tests/test_template_code_parity.py` garante que
todo `{{placeholder}}` nos templates tem o `kwarg` correspondente no dispatch.
Quebra o CI se um placeholder for adicionado sem atualizar o código — detecta o
problema ANTES do merge.

### Adicionar um novo estágio

1. Crie `flow/prompts/<estágio>.md` com os placeholders `{{variavel}}`.
2. Chame `render_prompt("<estágio>", fallback=..., **vars)` no `deployment.py`.
3. Adicione testes smoke em `flow/tests/test_prompts_loader.py` (classe `TestRealTemplates`).
4. O teste `test_template_code_parity.py` valida automaticamente a paridade — se quebrar, o `kwarg` está faltando no dispatch.
5. Após o merge, **reinstale o cron**: `./scripts/install-cron.sh`.

## Invariante de nome de branch (INVIOLÁVEL)

**Toda branch de trabalho da esteira DEVE ter o nome `feat/issue-<number>`.**

Nunca use nomes livres (`fix/...`, `chore/...`, `hotfix/...`). O motivo é que
o sistema de guarda usa o nome canônico para detectar colisão de worktree e
localizar a PR existente (`gh pr list --head feat/issue-N`). Um nome
alternativo escapa à detecção e pode criar uma segunda PR silenciosamente
(foi o que causou o bug #136 — sessão do reviewer inventou `fix/code-scanning-alerts-133`).

**Esta regra vale para TODOS os templates:**

| Template | Branch usada |
|----------|-------------|
| `develop_waiting.md` | cria `feat/issue-{{issue_number}}` |
| `rework.md` | usa a `feat/issue-{{issue_number}}` existente |
| `merge_conflict.md` | usa a `feat/issue-{{issue_number}}` existente |
| `review_waiting.md` | **não cria branch** — apenas lê e comenta |

Se uma sessão criar qualquer branch fora deste padrão, é um bug a reportar.
