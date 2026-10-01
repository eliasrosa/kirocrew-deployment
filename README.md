# kirocrew-flow

**KiroCrew Flow** — orquestração de esteira de desenvolvimento multi-squad sobre o
**[Kiro Crew](https://github.com/kirodotdev)**: os fluxos de trabalho são **grafos**,
o estado de cada task vive em **labels `flow:*`** na própria issue, e o polling
é **zero-token**.

> ⚠️ **Não é standalone.** Depende do Kiro Crew rodando na máquina: dispara sessões
> de agente via o webhook do dashboard (`POST /api/hooks/agent`) e usa o formato de
> *cron de script* do Kiro Crew. Veja [Dispatch de sessões](#dispatch-de-sessões-webhook) abaixo.

## Princípios

1. **A issue É o estado.** Label = estado atual, comentário = histórico auditável.
2. **Zero-token no polling.** O scan é Python puro — token só gasto quando há trabalho real.
3. **Merge é manual por padrão.** A automação abre o PR e para. Nenhum deploy automatizado. Auto-merge é opt-in por repo (`auto_merge: true` no `squads/*.yaml`) ou globalmente (`auto_merge_on_approve: true` em `workflow_params`).
4. **Gates humanos são invioláveis.** Aprovação de spec, review e QA são sempre de pessoas.
5. **Exceções são auditáveis.** O bypass do HML (hotfix direto pra PRD) exige justificativa e é rastreado.

## Modelo de labels

Duas dimensões independentes.

### Estados — 1 por vez, nesta ordem

```
flow:briefing → flow:planning-specs → flow:planning-review → flow:develop-waiting
  → flow:develop-running → flow:review-waiting → flow:review-approved
  → flow:qa-waiting → flow:qa-testing → flow:qa-approved → flow:done
```

> Labels de estado legadas (`crewflow:spec`, `crewflow:ready`, `crewflow:todo`, etc.)
> foram deprecadas. Use `setup-flow-labels.sh` para criar o namespace `flow:*` em novos repos.

### Modificadores — 0..N, sobrepõem ao estado

| Label | Significado |
|---|---|
| `flow:blocked` | Para tudo (prioridade sobre o estado) |
| `flow:merge-conflict` | PR com conflito de merge ou base desatualizada — cron resolve via rebase |

### Metadado crewflow:* (tipo de fluxo e prioridade)

`crewflow:feature` · `crewflow:bug` · `crewflow:hotfix` · `crewflow:debt`  
`crewflow:p1` · `crewflow:p2` · `crewflow:p3`

> `crewflow:blocked` é reconhecido pelo motor por compatibilidade com repos que já
> usam o namespace legado; o equivalente canônico é `flow:blocked`.

Aplique os estados num repo:
```bash
./scripts/setup-flow-labels.sh owner/repo
```

Aplique o metadado (tipo + prioridade):
```bash
./scripts/setup-labels.sh owner/repo
```

## Como funciona

O motor atual é **single-flow, ledger-driven** — uma cron única, custo O(1) por tick,
estado guardado em SQLite local (não mais nas labels do GitHub).

```
squads/voomp-squad-gw.yaml
    → run_single_flow(ctx)
        → ledger.active()          ← 1 query SQLite (nada de gh issue list)
        → provider.get_work_item() ← 1 chamada de rede (estado real da issue)
        → ledger_tick.tick()       ← decide + avança estágio
            → dispatcher.dispatch_stage() ← abre sessão one-shot quando necessário
```

1. **`ledger.active()`** — lê a task em execução em O(1) (1 registro SQLite). Se não há nada ativo e `claim_single_flow` ainda não reclamou uma issue, o tick é idle.
2. **`claim_single_flow`** — reclamou uma issue? Cria o `RunLedger` com o estado inicial e o `squad_id` resolvido. A partir daqui o estado vive no banco local.
3. **`ledger_tick.tick()`** — lê o estado do ledger, lê o sinal externo da issue (existe PR? review pronto?) e avança o estágio:
   - **Estágios ativos** (`BRIEFING`, `PLANNING_SPECS`): o motor dispara a sessão e avança.
   - **Estágios de espera** (`DEVELOP`, `REVIEW`, `QA`): o motor observa o sinal externo (PR aberto, review aprovado, QA ok) e avança quando o sinal chega.
4. A sessão one-shot **nunca mergeia e nunca faz deploy**. Ela entrega o PR e encerra.

> **Diferença do modelo label-driven legado:** antes o scan varria todas as issues abertas
> por estado (O(n)), com N crons por estágio. Hoje é 1 cron + 1 query SQLite + 1 chamada
> de rede, independente do volume de issues.

### Sequência de uma task no single-flow

```
flow-single tick (a cada 60s, zero token)
  │
  ├─ ledger.active() → sem task ativa
  │   └─ claim_single_flow() → issue VGAT-123 reclamada
  │       → ledger criado: estado=BRIEFING
  │
  ├─ ledger.active() → VGAT-123, estado=BRIEFING
  │   → dispatch_stage(BRIEFING) → sessão one-shot aberta
  │       Agente: monta contexto, define objetivo, fecha em PLANNING_SPECS
  │   → ledger avança: BRIEFING → PLANNING_SPECS
  │
  ├─ ledger.active() → VGAT-123, estado=PLANNING_SPECS
  │   → dispatch_stage(PLANNING_SPECS) → sessão one-shot aberta
  │       Agente: escreve spec, critérios de aceite, sub-tasks
  │   → ledger avança: PLANNING_SPECS → DEVELOP_WAITING
  │
  ├─ ledger.active() → VGAT-123, estado=DEVELOP_WAITING
  │   → aguarda PR aberto (sinal externo do dev)
  │   → PR detectado → ledger avança: DEVELOP_WAITING → REVIEW_WAITING
  │
  ├─ ledger.active() → VGAT-123, estado=REVIEW_WAITING
  │   → aguarda review aprovado
  │   → review aprovado → ledger avança: REVIEW_WAITING → QA_WAITING
  │
  └─ ledger.active() → VGAT-123, estado=QA_WAITING
      → aguarda QA
      → QA ok → ledger avança: QA_WAITING → DONE
          → ledger.release() → slot livre para próxima task
```

### Dispatch de sessões (loopback interno)

O dispatch faz dois POST ao gateway local do Kiro Crew:

1. `POST /api/chat/slots` — cria o slot de sessão e obtém o `tab_id`.
2. `POST /api/chat` — injeta a instrução renderizada na sessão criada.

O secret é lido de `~/.kiro/crew/run/gateway-{port}.secret` (atualizado a cada
restart), com fallback para `~/.kiro/crew/.local_secret`. O `ctx._secret` injetado
pelo runtime não é usado (fica stale após restart).

O `tab_id` retornado pelo Step 1 é gravado no ledger como `session_key`. A detecção
de encerramento da sessão é feita varrendo os arquivos `.jsonl` do gateway pelo
`tab_id` — o nome do arquivo JSONL não contém o slot name, apenas um número
sequencial interno.

## Fluxos disponíveis (Fase 1)

| Template | Quando usar |
|---|---|
| `feature` (Versão C) | Feature nova. Review ANTES do QA, sequencial. |
| `bug` | Correção de bug. Mesma ordem da Versão C. |
| `hotfix` | Incidente em PRD. GATE 0 filtra o que é realmente urgente. |
| `debt` | Refatoração sem mudança de comportamento. TL aprova; QA valida equivalência. |

Diagramas em [`docs/diagramas/`](docs/diagramas/README.md).

## Setup

Pré-requisito: Kiro Crew rodando + `gh` autenticado (`gh auth status`).

### 1. Configure a squad

Crie `squads/minha-squad.yaml` com base em `squads/example.yaml`:

```yaml
id: minha-squad
name: Squad Exemplo
issue_provider: jira   # ou github
project: VGAT          # chave do projeto Jira

# Repositórios da squad.
# Forma simples (herda flags globais): - org/api-gateway2
# Forma inline com flags por repo (auto_dispatch / auto_merge):
repos:
  - url: https://github.com/org/api-gateway2
    auto_dispatch: true
    auto_merge: false      # merge manual neste repo
  - url: https://dev.azure.com/kdop/Projeto/_git/api-subscription2
    auto_dispatch: true
    auto_merge: true       # merge automático após approve

workflow_template: versao-c
routing:
  - match:
      labels:
        - crewflow:hotfix
    workflow: hotfix-flow
  - default: feature-flow
```

> **Vários squads via `squads_dir` (#311):** no app instalado, em vez de apontar
> `squad_config` para um único arquivo, use `squads_dir` no `deployment.config.yaml`
> para um DIRETÓRIO de squads (ex: `~/.kiro/crew/crons/squads/`) — um YAML por
> squad/projeto, todos carregados automaticamente. `squad_config` (arquivo único)
> segue suportado como fallback. Detalhes e fluxo de onboarding em `config.example.yaml`.

> **`issue_provider` por repo (#311):** cada entrada de `repos:` aceita
> `issue_provider: github | jira` para sobrescrever o default da squad (override
> por-repo > default da squad). Azure DevOps é SCM-only — nunca é issue_provider.

> **Suporte a Azure DevOps (#266):** URLs `dev.azure.com/.../_git/<repo>` são detectadas
> automaticamente — o motor instancia `AzureDevOpsTransport` para operações de SCM
> (abertura de PR, verificação de CI, merge). Requer `AZURE_DEVOPS_PAT` em `.env`.
> GitHub continua sendo detectado por `github.com/` ou formato `owner/repo`.

> **PyYAML (recomendado para routing complexo):** o parser embutido (`_mini_yaml`) suporta
> escalares, listas simples, mapeamentos de 1 nível, e listas de dicts — tanto no formato
> inline (`{labels: [...]}`) quanto multi-linha. Para garantir compatibilidade total com YAML
> arbitrário, instale PyYAML:
>
> ```bash
> pip install "kirocrew-flow[yaml]"
> # ou: pip install pyyaml
> ```
>
> Sem PyYAML o fallback cobre os casos de uso do squad config padrão.

### 2. Configure o cron

```bash
# SEMPRE usar o script de instalação — não copie manualmente
./scripts/install-cron.sh

# O script copia deployment.py, deployment/flow/, flow_update_check.py,
# aplica o patch de sys.path e copia deployment.config.yaml (se não existir).
# Edite ~/.kiro/crew/crons/deployment.config.yaml com seus paths.
```

**Secrets via `.env` (#272):** os secrets saíram do `deployment.config.yaml` e
agora vivem em `.env` (gitignored). Copie `.env.example` para `.env` e preencha:

```bash
cp .env.example .env
# edite .env: AZURE_DEVOPS_PAT, KIROCREW_WEBHOOK_TOKEN, KIROCREW_WEBHOOK_SECRET
```

O `install-cron.sh` também copia `.env.example` como referência para
`~/.kiro/crew/crons/deployment.env.example`. O `.env` com valores reais **nunca**
é commitado.

Se o App estiver instalado via `kirocrew app enable kirocrew-flow`, os crons são
registrados automaticamente pelo gateway ao habilitar o App (via `app.json`).
Para instalar manualmente ou atualizar os scripts instalados:

```bash
./scripts/install-cron.sh
```

**Crons registrados automaticamente pelo App:**

| Nome | Script | Intervalo | O que faz |
|---|---|---|---|
| `flow-single` | `deployment/flow/single_flow.py:run` | 60s | Tick principal — ledger-driven, O(1) por ciclo |
| `flow-update-check` | `flow_update_check.py:run` | 3600s | Verifica canal `stable` e aplica política de update ([`docs/RELEASE.md`](docs/RELEASE.md)) |

> **Histórico:** antes da frente 6 (set/2026) havia 8 crons por estágio
> (`flow-develop-waiting`, `flow-review-waiting`, etc.) mais o monolítico
> `crewflow-scan`. Todos foram substituídos pelo `flow-single` único.
> O cron `flow-auto-update` também foi removido (update via hook `setup.onUpdate`
> do App, gated por [`docs/RELEASE.md`](docs/RELEASE.md)).

### 3. Aplique as labels

```bash
# Labels de estado (namespace flow:*)
./scripts/setup-flow-labels.sh owner/repo

# Labels de metadado (tipo de fluxo + prioridade, namespace crewflow:*)
./scripts/setup-labels.sh owner/repo
```

### 4. Comece no modo de aviso

Deixe `auto_dispatch: false` (só avisa). Quando confiar, mude para `true`.

## Estrutura do código

```
flow/
├── domain/      ← regras de negócio puras (sem I/O), testáveis sem mock
│   ├── state.py — Estado, Modificador, is_dispatchable()
│   └── gates.py — can_leave_spec(), triage_hotfix(), validate_hml_bypass()
├── ports/       ← contrato do provider (IssueProvider Protocol)
├── adapters/    ← GitHub e Jira (transport + normalization + client)
├── scan/        ← zero-token polling + cache SQLite
├── executor/    ← decide() por template (feature/bug/hotfix/debt)
├── audit/       ← comentário estruturado <!-- KIRO-FLOW-STATE -->
├── prompts/     ← templates MD editáveis por estágio (dev, reviewer, …)
└── config/      ← SquadConfig + workflow templates

deployment/      ← cron de script do Kiro Crew (driving adapter)
squads/          ← configurações de squad (*.yaml)
workflows/       ← templates de workflow (*.yaml)
resources/mermaid/ ← fonte dos diagramas (.mmd)
docs/            ← VISION.md, ARCHITECTURE.md, ROADMAP.md, diagramas/
```

Ver `.kiro/steering/arquitetura.md` para convenções de código e como adicionar um novo provedor.

### Editar o prompt de uma sessão one-shot

Os prompts ficam em `flow/prompts/`:
- `develop_waiting.md` — sessão de implementação (o agente que abre o PR)
- `review_waiting.md` — sessão de code review
- `merge_conflict.md` — resolução de conflito de merge

Edite o MD livremente. Placeholders usam `{{nome}}`. Se um placeholder referenciar
uma variável que o motor não fornece, o dispatch **falha explicitamente** (fail-closed)
em vez de mandar o prompt quebrado. Em caso de arquivo ausente, o motor usa o fallback
embutido em `deployment.py`.

> ⚠️ **Regra crítica — reinstale o cron após qualquer mudança em prompts ou deployment.py**
>
> Os templates (`flow/prompts/*.md`) são lidos em runtime, mas `deployment.py` é **copiado**
> para `~/.kiro/crew/crons/` na instalação. Um PR que adiciona `{{nova_var}}` a um template
> sem reinstalar o cron causa `PromptRenderError` em **todas** as issues do estágio afetado.
>
> ```bash
> ./scripts/install-cron.sh
> ```
>
> O CI detecta o descompasso **antes do merge** via `flow/tests/test_template_code_parity.py`.
> O cron detecta **em runtime** via `deployment.version` e notifica quando o script instalado
> diverge do repo.

### Comportamento do reviewer (`review_waiting.md`)

O agente reviewer valida o PR como **gate único** antes do approve:

1. **Lê o contexto completo** — issue, comentários da issue, diff do PR, comentários do PR.
2. **Verifica a pipeline de CI** — `gh pr checks` — o PR só pode ser aprovado com CI verde.
3. **Analisa o código** — corretude, testes, estilo e convenções do steering do repo.
4. **Decide com as três condições**: CI verde + zero comentários não resolvidos no PR + sem blockers técnicos.
5. **Posta o resultado completo nos DOIS lugares** — PR e issue — com: o que foi feito, o resultado, o link e todas as informações.
6. **Aplica `flow:review-running`** somente quando as três condições são satisfeitas. Com `auto_merge: true` no repo (ou `auto_merge_on_approve: true` global no squad config), o motor faz merge squash automático; sem a flag (default), para em `flow:review-running` aguardando merge manual.

## Desenvolvimento

```bash
# Lint
python3 -m ruff check flow/

# Testes + cobertura
python3 -m pytest flow/tests/ --cov=flow --cov-report=term-missing

# Tudo junto
python3 -m ruff check flow/ && python3 -m pytest flow/tests/ --cov=flow --cov-fail-under=75
```

630 testes, cobertura ≥75% (piso do CI), ruff limpo.

> Após o cleanup do modo legado (PR #319), a suite passou de 1121 para 827 testes —
> os testes dos crons por estágio foram removidos junto com o código que testavam.

## Dry-run — inspecionar sem despachar

O single-flow não tem dry-run nativo na linha de comando. Para inspecionar o estado
do ledger sem despachar, use o SQLite diretamente:

```bash
# Ver runs ativos
sqlite3 ~/.kiro/crew/crons/deployment/data/flow.db \
  "SELECT issue_key, state, squad_id FROM run_ledger WHERE status='active'"

# Ver histórico de avanços de uma issue
sqlite3 ~/.kiro/crew/crons/deployment/data/flow.db \
  "SELECT state, entered_at FROM run_ledger_history WHERE issue_key='VGAT-123' ORDER BY entered_at"
```

Para simular um tick sem efeitos colaterais, monte um `ctx` sintético e chame
`ledger_tick.tick()` diretamente — o motor é puro (sem I/O de rede), testável com
um `Dispatcher` fake.

## Roadmap

| Fase | Estado |
|---|---|
| **Fase 1** — fluxos fixos, sem editor | ✅ Concluída (set/2026) |
| **Fase 2** — editor read-only no dashboard | 🔲 Planejada |
| **Fase 3** — canvas editável estilo n8n | 🔲 Futura |

Ver [`docs/ROADMAP.md`](docs/ROADMAP.md) para detalhes.

## Smoke test do fluxo completo ✅

O fluxo `develop → review → merge` foi validado end-to-end via [issue #230](https://github.com/eliasrosa/kirocrew-flow/issues/230):

1. Issue entrou em `flow:develop-waiting` → cron despachou sessão sidebar.
2. Agente implementou e abriu PR com `flow:review-waiting`.
3. Reviewer automático analisou e aprovou → `flow:review-approved`.
4. Merge squash automático executado → issue fechada com `flow:done`.

## Segurança / privacidade

- Repos, chat_id e paths vivem no `config.yaml` (gitignored). O `config.example.yaml` só tem placeholders.
- O disparo usa o segredo interno do gateway apenas em loopback (localhost).

