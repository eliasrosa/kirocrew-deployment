# kirocrew-flow

**KiroCrew Flow** — orquestração de esteira de desenvolvimento multi-squad sobre o
**[Kiro Crew](https://github.com/kirodotdev)**: os fluxos de trabalho são **grafos**,
o estado de cada task vive em **labels `crewflow:*`** na própria issue, e o polling
é **zero-token**.

> ⚠️ **Não é standalone.** Depende do Kiro Crew rodando na máquina: usa o loopback
> interno do gateway (`POST /api/chat`) e o formato de *cron de script* do Kiro Crew.
> Pense nisso como uma **receita/plugin para o Kiro Crew**, não um app universal.

## Princípios

1. **A issue É o estado.** Não há banco de trabalho paralelo: label = estado atual,
   comentário estruturado = histórico. Distribuído nativamente e visível pro humano.
2. **Zero-token no polling.** O scan é Python puro — não acorda o agente. Token só
   é gasto quando há trabalho real.
3. **Merge é SEMPRE manual.** A automação abre o PR e **para**. Nenhum merge, nenhum
   deploy é automatizado. Auto-merge opcional por squad está no radar (futuro).
4. **Gates humanos são obrigatórios.** Aprovação de spec, de code review e de QA são
   sempre de pessoas.

## Modelo de labels

Duas dimensões independentes.

### Estados — 1 por vez, nesta ordem

```
crewflow:spec → crewflow:ready → crewflow:todo → crewflow:dev → crewflow:review → crewflow:qa → crewflow:done
```

| Label | Significado | Quem age |
|---|---|---|
| `crewflow:spec` | PM especificando | 🧠 humano |
| `crewflow:ready` | Spec pronta, aguardando priorização | 🧠 humano libera |
| `crewflow:todo` | **Priorizado — gatilho da esteira** | 🤖 automação |
| `crewflow:dev` | Em desenvolvimento | 🤖 automação |
| `crewflow:review` | PR aberto: 🤖 review prévio + TL aprova (**ANTES do QA**) | 🤖 + 🧠 TL |
| `crewflow:qa` | Deploy HML manual + QA testa (**DEPOIS do review**) | 🧠 Dev + QA |
| `crewflow:done` | Concluído | — |

### Modificadores — 0..N, sobrepõem ao estado

| Label | Significado |
|---|---|
| `crewflow:blocked` | Bloqueado — **para tudo** (tem prioridade sobre o estado) |
| `crewflow:running` | Trabalho em andamento no estado atual |
| `crewflow:reviewed` | Lock anti-loop: já analisado neste SHA |

### Tipo de fluxo (routing) e prioridade

`crewflow:feature` · `crewflow:bug` · `crewflow:hotfix` · `crewflow:debt`
`crewflow:p1` · `crewflow:p2` · `crewflow:p3`

Aplique todas num repo:
```bash
./scripts/setup-labels.sh owner/repo
```

## Como funciona

```
Cron de SCRIPT (zero token, a cada X min)
  ├─ varre os repos configurados por issue aberta com `crewflow:todo`
  │  (ignora quem tem `crewflow:dev`, `crewflow:running` ou `crewflow:blocked`)
  ├─ auto_dispatch=false → só AVISA (você aciona manual)   ← comece aqui
  └─ auto_dispatch=true  → dispara uma SESSÃO one-shot
        └─ implementa → valida local → abre PR → `crewflow:review` → PARA
```

A sessão **nunca mergeia e nunca faz deploy**. Ela entrega o PR no estado
`crewflow:review` e encerra. Tudo depois disso é humano.

**Por que sem loop:** a sessão é one-shot (`memory_mode: temporary`), roda uma vez
e morre. Sem watchdog, sem auto-nudge, sem rearme.

### Travas de segurança
- `max_concurrent` — máx de sessões simultâneas no total (default 2).
- `one_per_repo` — no máx 1 sessão ativa por repo.
- `max_turns_per_task` — teto duro de turnos por sessão.
- Worktree isolado (`git worktree`) a partir do clone local — não reclona, não
  toca o working tree/branches existentes.
- `auto_dispatch=false` por padrão — comece avisando, ligue o disparo quando confiar.

## Fluxo de trabalho (Versão C — oficial)

O template fixo da Fase 1. Code review vem **antes** do QA, sequencial:

1. PM especifica (`crewflow:spec`) → TL aprova a spec → `crewflow:ready`
2. Priorizado → `crewflow:todo` (**gatilho**)
3. Esteira implementa (`crewflow:dev`), valida local, abre PR → `crewflow:review`
4. 🤖 Code review automatizado comenta no PR e marca `crewflow:reviewed`
5. **TL aprova** o review (aprovação humana obrigatória)
6. **Dev faz deploy HML manualmente** e libera pra teste → `crewflow:qa`
7. QA testa em HML e aprova
8. **Merge manual** → `crewflow:done`

## Setup

Pré-requisito: Kiro Crew rodando + `gh` autenticado (`gh auth status`).

1. Copie e edite a config:
   ```bash
   cp config.example.yaml deployment/deployment.config.yaml
   # edite: repos, notify_chat_id, dev_root, limites
   ```
2. Coloque o script onde o Kiro Crew lê crons e registre:
   ```bash
   cp deployment/deployment.py ~/.kiro/crew/crons/deployment.py
   cp deployment/deployment.config.yaml ~/.kiro/crew/crons/deployment.config.yaml
   # via MCP cron_add (dashboard/CLI do Kiro Crew):
   #   name="crewflow-scan", script="~/.kiro/crew/crons/deployment.py:run", every=600
   ```
3. Aplique as labels nos seus repos (`scripts/setup-labels.sh`).
4. Comece com `auto_dispatch: false` (só avisa). Quando confiar, mude para `true`.

## Roadmap (fases)

| Fase | O que entrega |
|---|---|
| **Fase 1** | Fluxos **fixos** (feature = Versão C, + fluxo de bug), sem editor. Valida motor + rastreabilidade. |
| **Fase 2** | Editor de fluxos **read-only** — visualiza o grafo no dashboard. |
| **Fase 3** | Editor **editável** — canvas estilo n8n: arrastar nós, conectar, salvar por squad. |

O grafo do diagrama **É** a máquina de estados que o motor executa. Entre as fases
o comportamento não muda — só muda de *hardcoded* para *editável no canvas*.

## Segurança / privacidade

- Nada de dado pessoal no código: repos, chat_id e paths vivem no `config.yaml`
  (gitignored). O `config.example.yaml` só tem placeholders.
- O disparo usa o segredo interno do gateway apenas em loopback (localhost).
