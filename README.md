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
3. **Merge é manual por padrão.** A automação abre o PR e para. Nenhum deploy automatizado. Auto-merge é opt-in por repo via a flag `auto_merge` no squad config (com fallback global).
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

```
squads/*.yaml → SquadConfig → scan_candidates() → executor.decide() → deployment.run()
```

1. `scan_candidates()` varre as issues por labels `flow:*` sem gastar token — compara hash do estado atual com o cache SQLite, e só processa o que mudou.
2. `executor.decide()` decide a ação (DISPATCH_DEV, DISPATCH_REVIEWER, NOTIFY_HUMAN, BLOCK, REBRAND ou SKIP) com base no template da squad e no estado da issue.
3. `deployment.run()` executa a ação: dispara sessão one-shot, notifica humano ou aplica rebrand de template.

A sessão one-shot **nunca mergeia e nunca faz deploy**. Ela entrega o PR em `flow:review-waiting` e encerra.

### Multi-SCM — GitHub e Azure DevOps

A esteira suporta **GitHub e Azure DevOps** através de uma factory de transport
unificada (`flow/adapters/scm/factory.py` — `ScmTransportFactory` +
`ScmRepoConfig`). A mesma superfície de métodos (abrir PR, listar/mergear PR,
listar reviews, postar comentário, deletar branch) vale para os dois providers;
a factory delega ao transport correto por repo. Os transports vivem em
`flow/adapters/scm/`: `github.py` (re-exporta `flow/adapters/github_transport.py`,
que usa o `gh` CLI) e `azure_devops.py` (REST API do Azure DevOps, autenticada
por PAT).

O SCM de cada repo é resolvido de duas formas (ver `scm_config_from_repo_entry`
e `_infer_scm_from_name`):

- **Explícito** — declare `scm: azure_devops` na entrada do repo, junto com os
  campos `azure_org`, `azure_project` e `azure_repo`:

  ```yaml
  repos:
    - name: kdop/api-gateway2
      scm: azure_devops
      azure_org: https://dev.azure.com/kdop
      azure_project: PlataformaCogna-MKTP-MVP
      azure_repo: voomp-creators-api-gateway2
    - name: org/frontend
      scm: github          # default quando `scm` é omitido
  ```

- **Detecção automática** — quando `scm` é omitido, o SCM é inferido pela URL: um
  `name`/`url` que começa com `dev.azure.com/` (com ou sem `https://`) é tratado
  como `azure_devops`, e os campos `azure_org`/`azure_project`/`azure_repo` são
  derivados do padrão `dev.azure.com/<org>/<project>/<repo>`. Qualquer outro
  formato cai em `github`.

A autenticação do Azure DevOps usa a variável de ambiente `AZURE_DEVOPS_PAT`
(ver [`.env.example`](.env.example) e a seção [Segurança / privacidade](#segurança--privacidade)).

### Protocolo cron ↔ agente

**Cron Python e agente LLM não se chamam diretamente** — toda a comunicação acontece via labels `flow:*` na issue.

```
Cron Python (zero token)
  │  scan_candidates() detecta flow:develop-waiting
  ▼
  aplica flow:develop-running  ──────────────► [ issue atualizada ]
  _dispatch() dispara sessão one-shot ───────► Agente (gasta token)
                                                │  implementa + abre PR
                                                ▼
  scan_candidates() detecta mudança ◄───────── aplica flow:review-waiting
  │
  ▼
  aplica flow:review-running ────────────────► Agente reviewer (gasta token)
                                                │  lê PR + posta review
                                                ▼
  scan_candidates() detecta mudança ◄───────── aplica flow:review-approved
  │                                               (ou flow:review-refused → gate humano)
  ▼
  merge squash → flow:qa-waiting → … → flow:done
```

Tabela completa de labels (quem aplica e quando): [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#protocolo-cron--agente).  
Diagrama de sequência Mermaid: [`docs/fluxo.md`](docs/fluxo.md).

### Dispatch de sessões (webhook)

Os crons de estágio que precisam acordar uma sessão de agente (`flow-develop-waiting`,
`flow-review-waiting`, `flow-merge-conflict`) despacham via **POST ao webhook do
dashboard** do Kiro Crew. Isso funciona nos crons `script`-based, cujo `ScriptContext`
não expõe `_port`/`_secret` do gateway (a causa raiz da issue #212, em que a label era
trocada mas nenhuma sessão era criada).

Duas variáveis de ambiente controlam o transporte (registre-as como **Secrets do cron**
no dashboard → Schedule → cron → Secrets):

| Variável | Default | Descrição |
|---|---|---|
| `KIROCREW_WEBHOOK_URL` | `http://localhost:5478/api/hooks/agent` | Endpoint do webhook do dashboard. |
| `KIROCREW_WEBHOOK_TOKEN` | *(vazio)* | Token Bearer do webhook configurado (Settings → Webhooks). Nunca é hard-coded. |

Quando `KIROCREW_WEBHOOK_TOKEN` está setado, o dispatch faz
`POST {KIROCREW_WEBHOOK_URL}` com header `Authorization: Bearer <token>`. Quando o token
está **vazio**, cai no comportamento legado de loopback interno (`POST /api/chat` com
`X-Internal-Secret`/`X-Session-Key`), preservando os crons `message`-based. O scan em si
continua **zero-token** — o webhook só é chamado quando há um candidato real na fila.

**Secret interno do gateway (loopback).** No fallback de loopback, o segredo
interno é resolvido por precedência (issue #263): primeiro
`~/.kiro/crew/run/gateway-{port}.secret` (regenerado a cada restart do gateway),
depois `~/.kiro/crew/.local_secret` e, como último recurso, o `ctx._secret`
fixado no registro do cron. O `.local_secret` e o `ctx._secret` ficam
desatualizados após um restart do gateway e causavam `403` no dispatch — por
isso o `run/gateway-{port}.secret` vence.

**Slots órfãos (issue #267).** O dispatch de loopback faz duas chamadas: Step 1
(`POST /api/chat/slots`) cria o slot e Step 2 (`POST /api/chat`) envia a
mensagem. Quando o Step 2 falha depois de o slot já ter sido criado, o slot
órfão é **deletado automaticamente** (`_delete_orphan_slot` em
`deployment/deployment.py`), para não ficar aparecendo como uma sessão
'New Session...' vazia no sidebar do dashboard.

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
repos:
  - org/api-gateway2
  - org/api-subscription2
workflow_template: versao-c
routing:
  - match:
      labels:
        - crewflow:hotfix
    workflow: hotfix-flow
  - default: feature-flow
```

> **Config por repo (`auto_dispatch` / `auto_merge`).** Cada entrada de `repos:`
> pode ser um mapa com `url` (obrigatório) e os flags opcionais `auto_dispatch`
> e `auto_merge`, sobrepondo os defaults globais para aquele repo (issue #264).
> A `url` aceita o formato completo (`github.com/...`, `dev.azure.com/...`) ou o
> `owner/repo` puro:
>
> ```yaml
> repos:
>   - url: https://github.com/org/api-gateway2
>     auto_dispatch: true
>     auto_merge: false   # merge manual em PRD
>   - url: https://dev.azure.com/kdop/PlataformaCogna-MKTP-MVP/_git/api-subscription2
>     auto_dispatch: true
>     auto_merge: true
> ```
>
> A forma legada `repos_config:` (issue #245) é equivalente e usa `name` no lugar
> de `url`. Quando o mesmo repo aparece nas duas fontes, a entrada inline em
> `repos:` vence. Omitir um flag = herda o global. Ver `squads/example.yaml`.

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
# aplica o patch de sys.path, copia deployment.config.yaml (se não existir) e
# semeia ~/.kiro/crew/crons/.env a partir de deployment/.env.example (secrets).
# Edite ~/.kiro/crew/crons/deployment.config.yaml com seus paths e o .env com os secrets.
```

Se o App estiver instalado via `kirocrew app enable kirocrew-flow`, os crons são
registrados automaticamente pelo gateway ao habilitar o App (via `app.json`).
Para instalar manualmente ou atualizar os scripts instalados:

```bash
./scripts/install-cron.sh
```

**Crons registrados automaticamente pelo App (namespace `flow:*`):**

| Nome | Script | Intervalo | Estágio |
|---|---|---|---|
| `flow-develop-waiting` | `deployment/flow/dev.py:run` | 300s | `flow:develop-waiting` → implementa + PR |
| `flow-review-waiting` | `deployment/flow/reviewer.py:run` | 180s | `flow:review-waiting` → code review |
| `flow-review-approved` | `deployment/flow/review_approved.py:run` | 120s | `flow:review-approved` → merge → QA |
| `flow-review-refused` | `deployment/flow/rework.py:run` | 3600s | `flow:review-refused` → notifica TL |
| `flow-merge-conflict` | `deployment/flow/conflict.py:run` | 300s | `flow:merge-conflict` → rebase |
| `flow-qa-waiting` | `deployment/flow/qa_notify.py:run` | 600s | `flow:qa-waiting` → notifica QA |
| `flow-qa-approved` | `deployment/flow/qa_approved.py:run` | 120s | `flow:qa-approved` → merge → done |
| `flow-qa-refused` | `deployment/flow/qa_refused.py:run` | 3600s | `flow:qa-refused` → notifica TL+dev |
| `flow-update-check` | `flow_update_check.py:run` | 3600s | verifica canal `stable` e aplica a política de update ([`docs/RELEASE.md`](docs/RELEASE.md)) |

Para registrar manualmente (cron monolítico legado, todos os estágios em sequência):

```
cron_add(name="crewflow-scan", script="~/.kiro/crew/crons/deployment.py:run", every=600)
```

> **Update do App.** O antigo cron `flow-auto-update` (que fazia `git pull --rebase`
> incondicional a cada 5 minutos e podia auto-quebrar produção) foi **removido**. O
> update passa pelo caminho oficial do Crew App — o hook `setup.onUpdate` do
> `app.json` — e é **gated** pela política _auto vs manual_ de
> [`docs/RELEASE.md`](docs/RELEASE.md): apenas releases de **patch** (`fix`)
> auto-aplicam; releases **minor/major** (`feat`/breaking) apenas **notificam** e
> aguardam ação manual. O cron `flow-update-check` compara a versão instalada com o
> canal `stable` e delega a decisão à lógica pura `flow.domain.update_policy` — nunca
> puxa o `main` cegamente.

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
6. **Aplica `flow:review-running`** somente quando as três condições são satisfeitas. Com `auto_merge: true` para o repo no squad config, o motor faz merge squash automático; sem a flag (default), para em `flow:review-running` aguardando merge manual. O `auto_merge` é resolvido por repo (config inline em `repos:` ou legado em `repos_config:`), com fallback para a flag global de auto-merge do `workflow_params`.

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

## Dry-run — inspecionar sem despachar

Antes de ativar o `auto_dispatch`, use o modo dry-run para validar o que o motor faria:

```bash
CREWFLOW_DRY_RUN=1 python3 deployment/deployment.py
```

Ou via config (`deployment.config.yaml`):

```yaml
dry_run: true
```

Saída esperada:

```
[DRY-RUN] ──────────────────────────────────────────
[DRY-RUN] 2 issue(s) processada(s) pelo scan
[DRY-RUN] Decisões (nenhuma será executada):

[DRY-RUN] owner/repo#73 → DISPATCH_DEV (template via executor) — [repo] feat: dry-run
[DRY-RUN] owner/repo#74 → NOTIFY_HUMAN tl — [repo] Fix: aguarda gate-tl

[DRY-RUN] ── Nenhuma sessão despachada, label alterada ou notificação enviada. ──
```

Garantias do modo dry-run:
- O scan roda normalmente (lê issues, executa o executor, decide ações)
- `_dispatch()` **não** é chamado — nenhuma sessão one-shot é aberta
- `provider.set_labels()` **não** é chamado — nenhuma label é alterada
- `ctx.notify()` **não** é chamado — nenhuma notificação é enviada
- Nenhum lock é criado

## Roadmap

| Fase | Estado |
|---|---|
| **Fase 1** — fluxos fixos, sem editor | ✅ Concluída (set/2026) |
| **Fase 2** — editor read-only no dashboard | 🔲 Planejada |
| **Fase 3** — canvas editável estilo n8n | 🔲 Futura |

Ver [`docs/ROADMAP.md`](docs/ROADMAP.md) para detalhes.

## Release

As releases são dirigidas pelos **conventional commits** via
[`python-semantic-release`](https://python-semantic-release.readthedocs.io/): `feat`
gera bump **minor**, `fix` gera **patch** e `feat!:`/`BREAKING CHANGE:` geram **major**
(commits `docs`/`chore`/`refactor`/`test`/`style`/`ci` não cortam release). A pipeline
`.github/workflows/release.yml` roda no GitHub Actions **após o CI passar** no `main` —
ela é disparada por `workflow_run` do workflow `CI` e só prossegue se a conclusão foi
`success`, de modo que um `main` vermelho nunca corta uma release. A cada release ela
calcula a próxima versão, atualiza o CHANGELOG e cria a tag `vX.Y.Z`.

Há dois canais de release:

| Canal | Como avança | Uso |
|---|---|---|
| `latest` | Movido **automaticamente** pela pipeline a cada release. | Ponta — pode conter instabilidade. |
| `stable` | Promovido **manualmente** após validação. | Versão validada para produção. |

O `app.json` fixa `"channel": "stable"` por padrão, então uma instalação padrão do
App consome o canal validado; o cron `flow-update-check` compara a versão instalada
com esse canal e aplica a política de update. Consulte [`docs/RELEASE.md`](docs/RELEASE.md)
para a política completa (auto vs manual, fontes de verdade da versão e mecanismo de update).

## Smoke test do fluxo completo ✅

O fluxo `develop → review → merge` foi validado end-to-end via [issue #230](https://github.com/eliasrosa/kirocrew-flow/issues/230):

1. Issue entrou em `flow:develop-waiting` → cron despachou sessão sidebar.
2. Agente implementou e abriu PR com `flow:review-waiting`.
3. Reviewer automático analisou e aprovou → `flow:review-approved`.
4. Merge squash automático executado → issue fechada com `flow:done`.

## Segurança / privacidade

- Repos, chat_id e paths vivem no `deployment.config.yaml` (gitignored). O `config.example.yaml` só tem placeholders.
- **Secrets NÃO ficam no `deployment.config.yaml`.** Eles saem por variáveis de
  ambiente / `.env` (gitignored): `KIROCREW_WEBHOOK_TOKEN`, `KIROCREW_WEBHOOK_SECRET`
  e `AZURE_DEVOPS_PAT`. Use [`.env.example`](.env.example) como template para
  desenvolvimento local. O `deployment/.env.example` é o template equivalente que o
  `install-cron.sh` semeia em `~/.kiro/crew/crons/.env`; ambos listam o mesmo conjunto
  de secrets. Ao adicionar um secret, atualize os dois arquivos.
- O disparo usa o segredo interno do gateway apenas em loopback (localhost), com a
  precedência de secret descrita em [Dispatch de sessões](#dispatch-de-sessões-webhook).

