# KiroCrew Flow — Arquitetura

## Stack

| Item | Valor |
|---|---|
| Linguagem | Python 3.12 |
| Servidor web | aiohttp |
| Testes | pytest + pytest-cov |
| Linting | ruff |
| Type check | mypy |
| Estado do motor | SQLite (stdlib) via yoyo-migrations |
| YAML | PyYAML com fallback para parser interno |

Sem FastAPI, Flask, Starlette ou Pydantic — o projeto segue a stack do próprio
Kiro Crew, validada lendo o código instalado.

## Visão geral do motor

O motor atual é **single-flow, ledger-driven** (frentes 6–8, set/2026). Uma cron
única (`flow-single`, 60s) processa a task ativa em O(1) por tick: 1 query SQLite
+ 1 chamada de rede.

```
deployment/
├── deployment.py          ← driving adapter (run_single_flow + claim_single_flow)
└── flow/
    └── single_flow.py     ← entrypoint do cron

flow/
├── engine/
│   ├── ledger_tick.py     ← motor puro: tick(), máquina de estados
│   └── run_ledger.py      ← RunLedger (SQLite), RunStatus, State
├── domain/                ← NÚCLEO: sem I/O, testável sem mock
├── ports/                 ← contratos (Protocol)
├── adapters/              ← GitHub, Jira, SCM (AzureDevOps)
├── scan/                  ← LedgerStateReader (lê estado real da issue)
└── config/                ← squad config + workflow templates
```

## Padrão arquitetural: hexagonal (ports & adapters)

O domínio (`flow/domain/`) não tem I/O — é testável sem mock de rede ou banco.

```
flow/
├── domain/    ← NÚCLEO: sem I/O, testável sem mock
├── ports/     ← contratos (Protocol)
├── adapters/  ← GitHub, Jira, SCM
├── engine/    ← motor ledger-driven + RunLedger
├── scan/      ← LedgerStateReader (zero-token)
├── executor/  ← regras de transição por template (ainda em uso em alguns fluxos)
├── audit/     ← histórico de auditoria
└── config/    ← squad config + workflow templates
```

`test_domain_boundary.py` verifica por AST que nenhum arquivo em `flow/domain/`
importa infraestrutura. Se quebrar, pare tudo e conserte primeiro.

## Motor: ledger_tick

O motor vive em `flow/engine/ledger_tick.py`. É **síncrono e puro** — sem I/O de
rede. Recebe um `RunLedger`, um `WorkItem` (estado real da issue) e um
`Dispatcher` (Protocol), e decide o que fazer:

```python
def tick(
    ledger: RunLedger,
    work_item: WorkItem,
    dispatcher: Dispatcher,
) -> TickResult:
    ...
```

### Máquina de estados (single-flow)

```
BRIEFING → PLANNING_SPECS → PLANNING_REVIEW → DEVELOP_WAITING
         → REVIEW_WAITING → QA_WAITING → DONE

Terminais: DONE, REVIEW_REFUSED, QA_REFUSED
```

**Estágios ativos** (`_ACTIVE_STAGES`): `BRIEFING`, `PLANNING_SPECS` — o motor
dispara a sessão e avança.

**Estágios de espera**: `DEVELOP_WAITING`, `REVIEW_WAITING`, `QA_WAITING` — o
motor observa o sinal externo (PR aberto, review aprovado, QA ok) e avança quando
o sinal chega.

### Protocolo WORKFLOW_EXIT

O agente one-shot sinaliza o resultado ao encerrar publicando uma tag na última
mensagem do turno:

```
WORKFLOW_EXIT: {"exit_status": "done", "pr_url": "..."}
```

O motor lê o arquivo JSONL da sessão de trás para frente, extrai o último
`WORKFLOW_EXIT` e avança o ledger conforme o mapeamento de transições do nó.

### RunLedger (SQLite)

O estado da task ativa vive no `RunLedger` — banco SQLite local em
`~/.kiro/crew/crons/deployment/data/flow.db`. Esquema gerenciado por
`yoyo-migrations` (`deployment/migrations/`).

| Tabela | O que guarda |
|---|---|
| `run_ledger` | Run ativa: `issue_key`, `state`, `session_key`, `squad_id`, `status` |
| `run_ledger_history` | Histórico de transições com timestamp |

**Não há mais estado nas labels do GitHub/Jira.** As labels `flow:*` continuam
sendo aplicadas (para visibilidade), mas o estado canônico é o SQLite.

### Dispatcher (Protocol)

```python
@runtime_checkable
class Dispatcher(Protocol):
    def dispatch_stage(self, stage: State, issue_key: str, ...) -> str:
        """Dispara a sessão one-shot e retorna o session_key."""
        ...
```

`deployment.py` implementa o `Dispatcher` real. Os testes usam um `FakeDispatcher`.

## Resolução de squad_id e project

`claim_single_flow()` resolve o `squad_id` a partir do squad config
(`~/.kiro/crew/crons/squads/<squad>.yaml`). O `squad_id` é gravado no
`RunLedger` e usado pelo motor para:

1. Encontrar os repos corretos (SCM: ADO ou GitHub).
2. Renderizar o template de instrução da sessão.
3. Identificar o `issue_provider` (Jira ou GitHub).

## Dispatch de sessão

O dispatch faz dois POST ao gateway local do Kiro Crew:

**Step 1 — criar slot:**
```
POST /api/chat/slots
X-Internal-Secret: <secret>
{"slot": "engine-<run_id>-<node_id>", "mode": "one_shot"}
```

**Step 2 — injetar instrução:**
```
POST /api/chat
X-Internal-Secret: <secret>
X-Session-Key: dashboard:<slot>
{"message": "<instrução renderizada>"}
```

O secret é lido de `~/.kiro/crew/run/gateway-{port}.secret` (atualizado a cada
restart), com fallback para `~/.kiro/crew/.local_secret`.

O `tab_id` retornado pelo Step 1 é gravado como `session_key` no ledger. A
detecção de encerramento da sessão (`_session_is_closed`) varre os arquivos
`.jsonl` do gateway pelo `tab_id` (o nome do arquivo não contém o slot name).

**Fix slots órfãos:** quando o Step 2 falha após o Step 1 ter criado o slot, o
dispatch deleta o slot automaticamente para evitar sessões fantasmas no sidebar.

## Adapters são módulos, não classes

```python
_PROVIDERS = {"github": github_client, "jira": jira_client}
provider_for("jira")  # → o módulo jira_client
```

O gate de conformidade é `test_provider_parity.py` — compara GitHub (referência)
contra Jira. Um terceiro adapter que entre no dispatch sem registro no teste
falha o CI.

Cada adapter tem três camadas:

| Camada | Papel |
|---|---|
| `*_transport.py` | I/O bruto — fachada patchável nos testes |
| `*_normalization.py` | payload do provedor → contrato canônico |
| `*_client.py` | orquestração; satisfaz `IssueProvider` |

O `github_client` expõe funções extras (GitHub-only):
- `get_pr_checks(project, pr_number)` — checks de CI
- `upsert_pr_review_comment(...)` — cria/atualiza comentário de review
- `merge_pull_request(...)` — merge squash via API

## Identidade: projeto + issue key

A chave primária de um item de trabalho é **projeto + issue key** (`VGAT-123`).
O repo é derivado do título (`[repo] Descrição`), não da identidade.

## Labels: visibilidade, não estado canônico

As labels `flow:*` **ainda são aplicadas** para visibilidade no board do GitHub/Jira,
mas o estado canônico é o SQLite (`run_ledger.state`). Uma label desatualizada
não corrompe o motor — o próximo tick lê o ledger, não a label.

Estados (namespace `flow:*`, aplicados como espelho do ledger):
```
flow:briefing → flow:planning-specs → flow:planning-review → flow:develop-waiting
  → flow:review-waiting → flow:qa-waiting → flow:done
```

Modificadores (ainda usados pelo motor para decisões):
```
flow:blocked         # para o fluxo
flow:merge-conflict  # PR com conflito de merge
```

Metadado (tipo e prioridade, namespace `crewflow:*`):
```
crewflow:feature / crewflow:bug / crewflow:hotfix / crewflow:debt
crewflow:p1 / crewflow:p2 / crewflow:p3
```

> **Legado:** antes da frente 6, o estado canônico vivia nas labels (modelo
> label-driven). O mecanismo de `scan_candidates()` + `executor.decide()` +
> `KIRO-FLOW-STATE` comment foi substituído pelo ledger SQLite. O código legado
> foi removido na PR #319 (out/2026).

## Workspace isolado por task

Cada dispatch cria um **worktree efêmero** dedicado:

```
<dev_root>/<repo-short>/                 ← clone-base (nunca tocado)
<dev_root>/.esteira-worktrees/
    <repo-short>-<issue_number>/         ← worktree efêmero
```

`_worktree_path(dev_root, repo, issue_number)` é a **fonte única de verdade** —
usada pelo dispatch e pelo prompt da sessão one-shot.

## Detalhes para desenvolvedores

Ver `.kiro/steering/arquitetura.md` — cobre convenções de código (StrEnum,
`slots=True`, imports no topo, `with` múltiplos), como adicionar um novo
provedor, e como rodar o CI localmente.
