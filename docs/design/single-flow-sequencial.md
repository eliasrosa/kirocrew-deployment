# Design — Modo Single-Flow Sequencial

> Status: **DRAFT / desenho aprovado em conversa** — pré-implementação.
> Objetivo: levar **uma task por vez** do começo (especificação) ao fim (merge),
> passando pelos estágios em sequência, com estado local acompanhável e migrável
> para um store global no futuro.
>
> Nada do que existe hoje é desmanchado. Este modo é **opt-in** por config; o
> modo paralelo atual (multi-issue, orientado a estágio) segue como default.

---

## 1. Motivação

Hoje o motor é **orientado ao estado global do repo**: a cada tick o scanner
varre todas as issues, classifica por label e o dispatcher pega quem estiver no
estado-gatilho (`flow:develop-waiting`), respeitando `max_concurrent` /
`one_per_repo`. Resultado: várias issues em estágios diferentes ao mesmo tempo.

Dores observadas:

- Difícil acompanhar "onde cada task está agora" — o estado vive espalhado em
  labels + PR + comentário `KIRO-FLOW-STATE`.
- Falhas ficam invisíveis: o dispatcher já pulou para outra issue.
- Não há uma visão única de "o que está rodando / o que está parado".

O modo single-flow ataca isso levando **uma task inteira** pelo pipeline antes de
pegar a próxima, com um **ledger local** como fonte de verdade do "onde está".

---

## 2. Escopo

### Entra
1. Modo `single_flow` opt-in: prende **uma** task e a empurra estágio a estágio.
2. Início na **especificação** (`flow:briefing`), não no `develop-waiting`.
3. Dois estágios de agente **novos**: `briefing` e `planning-specs`.
4. Modelo de **2 sub-tasks fixas** por task: Especificação + Implementação.
5. `RunLedger` (SQLite local) atrás de interface, migrável para store global.
6. Extensão do **port** `IssueProvider` com suporte a sub-tasks (GitHub + Jira).

### Não entra (fica intacto)
- Motor de estados (`flow/domain/state.py`), gates, executor, prompts existentes,
  adapters GitHub/Jira da esteira de `develop-waiting` em diante.
- Modo paralelo atual — continua default.

### Fora do hexagonal (específico da squad, não vira contrato do port)
- Estimativa (pontos/horas), regra de entrada em sprint. É convenção interna da
  squad, tratada na camada de squad/prompt — **não** no core genérico.

---

## 3. Fluxo alvo (single-flow)

```
TASK criada
   │
   ▼
[flow:briefing]        ──► sessão DEV: briefing — entende a task            (NOVO estágio de agente)
   │
   ▼
[flow:planning-specs]  ──► sessão DEV: monta a spec (requirements+design+tasks)
   │                       dentro da Sub-task 1                             (NOVO estágio de agente)
   ▼
[flow:planning-review] ──► GATE HUMANO: TL/PM aprovam a Especificação       (já notifica hoje)
   │  (Sub-task 1 aceita)
   ▼
[flow:develop-waiting] ──► esteira de desenvolvimento (JÁ EXISTE HOJE)
   │
   ▼  develop-running → review-waiting → review-running → review-approved
   ▼  → (auto-merge) → qa-waiting → done
```

Os estados `BRIEFING`, `PLANNING_SPECS`, `PLANNING_REVIEW` **já existem** em
`flow/domain/state.py`; hoje só disparam `NOTIFY_HUMAN`. O trabalho é dar a eles
estágios de agente (prompt + entrypoint), espelhando o padrão de
`develop_waiting.md` + `deployment/flow/develop_waiting.py`.

---

## 4. Modelo de 2 sub-tasks

```
TASK (issue pai)
 ├── Sub-task 1 · "Especificação"
 │     Conteúdo: requirements + design + tasks (padrão Kiro, 3 seções numa sub-task só)
 │     Cobre os estados: briefing → planning-specs → planning-review
 │     Aceite: TL/PM aprovam → Sub-task 1 marcada como aceita
 │     (squad: carrega estimativa/pontos/horas próprios — fora do hexagonal)
 │
 └── Sub-task 2 · "Implementação"
       Cobre: develop-waiting em diante (a esteira que já roda hoje)
       (Cogna: estimativa/pontos/horas próprios — fora do hexagonal)
```

**Regra de avanço:** a task pai só transiciona `planning-review → develop-waiting`
quando a **Sub-task 1 (Especificação) estiver aceita**.

**Por que 2 e não 3:** requirements/design/tasks são um trabalho contínuo de
spec — o dev faz os três juntos. Uma sub-task "Especificação" com as três seções
dá um prazo estimável único para a fase de entendimento; "Implementação" dá outro.
Mais fiel ao trabalho real e mais limpo para o burndown.

---

## 5. Extensão do port (hexagonal)

O port `IssueProvider` hoje não expõe sub-tasks — só `parent_key` no item
normalizado. Para "Sub-task 1 aceita" funcionar em Jira **e** GitHub, estende-se
o contrato:

```
list_subtasks(project, key) -> list[dict]      # filhas da issue pai, normalizadas
get_subtask_acceptance(subtask) -> bool         # sub-task está aceita? (== fechada)
```

Implementações — **aceite = sub-task FECHADA** (uniforme nos dois):

| Provider | Sub-tasks | Aceite |
|----------|-----------|--------|
| Jira     | sub-tasks nativas | sub-task fechada/resolvida |
| GitHub   | sub-issues | sub-issue com `state == closed` |

O gate novo `can_leave_planning(item, subtasks)` verifica que a Sub-task 1
(Especificação) está fechada. Puro Python, sem I/O — como os demais gates.

---

## 6. Estado local — RunLedger

Fonte de verdade do "onde a task está", separada das labels (que continuam sendo
aplicadas para auditoria/GitHub).

```
interface RunLedger:
    claim(task_key, repo) -> bool        # prende UMA task (single-flow)
    active() -> Run | None               # a task presa, se houver
    advance(task_key, new_stage)         # registra transição
    release(task_key, status)            # libera o slot (done/refused/failed)
    all() -> list[Run]                   # visão de tudo (futuro OTL)
```

Implementação **agora**: `SqliteRunLedger`, reusando o padrão de
`flow/scan/cache.py`. Tabela:

```
active_run(
  task_key, repo, current_stage, stage_session,
  started_at, last_transition, attempts, status
)
```

Implementação **futura** (frente separada): `GlobalRunLedger` — Postgres ou API
compartilhada, para o TL ter a visão de todas as tasks rodando/paradas em todas as
squads. **Só uma nova implementação da mesma interface** — o motor não muda.

Regra do single-flow: o tick só considera uma nova task se `active()` for `None`.
Pega uma (maior prioridade / mais antiga em `briefing`), faz `claim`, e a partir daí
só empurra ela. Libera o slot em `done` ou gate humano de reprovação.

---

## 7. Config

```yaml
# deployment.config.yaml
single_flow: true          # opt-in; default false (mantém modo paralelo)
stall_timeout_min: 30      # estágio parado > N min → notifica (acompanhamento)
```

Quando `single_flow: false` (default), nada muda: o driver itera candidatos como
hoje. Quando `true`, o driver resolve a task presa no ledger e passa **só ela** ao
`executor.decide()`.

---

## 8. Aprovação da Especificação (gate planning-review)

- **Hoje:** por comentário na issue com marcador `KIRO-FLOW-STATE` (mesmo padrão
  que o débito técnico usa com `gate-tl`). Funciona no Telegram e no GitHub.
- **Futuro (interface, standby):** botão de aprovar no front-end. O `RunLedger`
  é desenhado para suportar isso — o botão vira apenas mais um caminho que grava
  a aprovação no ledger. Documentado aqui como ideia; **não é prioridade agora**.

---

## 9. Pontos de contato no código (para a implementação)

- `flow/domain/state.py` — estados de spec já existem; nada a criar aqui.
- `flow/prompts/` — novos: `briefing.md`, `planning_specs.md`.
- `deployment/flow/` — novos entrypoints: `briefing.py`, `planning_specs.py`.
- `flow/ports/issue_provider.py` — estender com sub-tasks.
- `flow/adapters/github_client.py` e `jira_client.py` — implementar sub-tasks.
- `flow/domain/gates.py` — novo `can_leave_planning`.
- `flow/domain/run_ledger.py` (novo) — interface + `SqliteRunLedger`.
- `deployment.py run()` — ramo `single_flow`: resolver task presa, empurrar. **[FEITO — frente 4]** Implementado no `_run_stage` via os entrypoints `run_briefing`/`run_planning`/`run_planning_review` (stages `_STAGE_BRIEFING`/`_STAGE_PLANNING`/`_STAGE_PLANNING_REVIEW`). Em `planning-review`, o driver lê as sub-tasks (`_spec_accepted_for`), passa `spec_accepted` ao `decide()`, que emite `ADVANCE_TO_DEVELOP`; a transição é registrada no `RunLedger` (`_ledger_advance`).
- `deployment.config.yaml` + `squads/*.yaml` — flags novas.

### Frente 7 — motor ledger-driven (uma cron, O(1)/tick) **[FEITO]**

Contexto: as frentes 5/6 fizeram a cron única varrer os 8 estágios chamando
`_run_stage` por estágio; cada `_run_stage` roda `scan_candidates`, que lista
issues **por estado** (16 estados). Resultado: ~128 chamadas `gh` por tick, mesmo
com a fila vazia → `RuntimeError: Script timed out after 30s`.

Solução: inverter a fonte de verdade para o `RunLedger` (SQLite local). O motor
`flow/engine/ledger_tick.py:tick` lê a task ativa (1 query local), lê o estado
REAL da issue via `provider.get_work_item` (1 chamada de rede), e a empurra um
passo por tick pela máquina de estados linear `_NEXT_STATE`. Custo O(1) por tick.

- `flow/engine/ledger_tick.py` (novo) — `tick(ledger, dispatcher, reader)` puro +
  Protocols `Dispatcher`/`StateReader`. Estágios ativos (`briefing`/`planning-specs`)
  são disparados pelo motor; estágios de espera (`develop`/`review`/`qa`) avançam
  quando o estado real da issue já passou do estágio (sinal externo).
- `deployment.py` — adapters concretos `_LedgerStateReader` (via `parse_state`) e
  `_LedgerDispatcher` (mapeia `State` → `_dispatch_briefing`/`_dispatch_planning`);
  `run_single_flow` agora chama `_single_flow_tick` (só com `single_flow: true`),
  não varre mais estágios.
- `deployment/flow/single_flow.py` — delega a `deployment.run_single_flow`.
- Testes: `flow/tests/test_ledger_tick.py` (11) + `TestRunSingleFlow`/
  `TestSingleFlowCronModule` reescritos para o modelo ledger-driven.

Nota: as labels `flow:*` seguem aplicadas para auditoria/visão humana no GitHub,
mas **não** dirigem mais o motor single-flow — o estado é o `RunLedger`.

---

## 10. Decisões fechadas

1. **"Sub-task aceita" = sub-issue FECHADA.** Uniforme em GitHub e Jira: fechar a
   sub-task marca o aceite. Não depende de label ou status extra. `get_subtask_acceptance`
   verifica `state == closed`.
2. **Estimativa: MANUAL, por enquanto.** Fora do escopo deste ciclo. O agente do
   `planning-specs` **não** sugere pontos/horas; humano estima. Refina em ciclo futuro.
3. **Ordem de implementação** — respeitando as dependências técnicas:
   - **Paralelo (independentes):** `RunLedger` SQLite  ·  estágios de agente
     `briefing` + `planning-specs`.
   - **Depende dos dois acima:** sub-tasks no port + gate `can_leave_planning`.
   - **Por último (amarra tudo):** ramo `single_flow` no `deployment.py run()`.

## 10b. Restrição inviolável

**Não mexer no que está funcionando agora.** A esteira de `develop-waiting` em
diante (dispatch, review, merge, QA) e o modo paralelo default permanecem
intocados. Toda adição é aditiva e sob a flag `single_flow` (opt-in). Rodar a
suite completa de CI local (`ruff check flow/`, `mypy flow/ --ignore-missing-imports`,
`pytest flow/tests/`) antes de qualquer push.

---

## 11. Frentes futuras (documentadas, não neste ciclo)

- **Store global (visão OTL):** `GlobalRunLedger` compartilhado entre squads.
- **Front-end:** Kanban/visão de execução + botão de aprovar spec. Hoje em standby.
