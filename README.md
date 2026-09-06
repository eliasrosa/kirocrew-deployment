# kirocrew-deployment

Esteira de desenvolvimento autônoma para o **[Kiro Crew](https://github.com/kirodotdev)**:
uma vigia zero-token que observa issues marcadas `aguardando-desenvolvimento` em vários repositórios e,
opcionalmente, dispara uma **sessão de execução one-shot** que implementa a issue,
abre o PR e mergeia — uma passada, sem loop.

> ⚠️ **Não é standalone.** Depende do Kiro Crew rodando na máquina: usa o loopback
> interno do gateway (`POST /api/chat`) e o formato de *cron de script* do Kiro Crew.
> Pense nisso como uma **receita/plugin para o Kiro Crew**, não um app universal.

## Como funciona

```
Cron de SCRIPT (zero token, a cada X min)
  ├─ varre os repos configurados por issue aberta com label `aguardando-desenvolvimento`
  │  (ignora quem já tem `em-desenvolvimento`)
  ├─ auto_dispatch=false → só AVISA (você aciona manual)   ← Fase 1, seguro
  └─ auto_dispatch=true  → dispara uma SESSÃO one-shot      ← Fase 2
        └─ implementa → PR → merge → limpa labels → ENCERRA (sem loop)
```

**Por que zero-token:** o polling é um script Python puro — não acorda o agente.
Só gasta token quando há trabalho real e uma sessão é disparada.

**Por que sem loop:** a sessão é one-shot (`memory_mode: temporary`), roda uma vez
e morre. Sem watchdog, sem auto-nudge, sem rearme.

### Travas de segurança
- `max_concurrent` — máx de sessões simultâneas no total (default 2).
- `one_per_repo` — no máx 1 sessão ativa por repo (evita colisão de merge na main).
- `max_turns_per_task` — teto duro de turnos por sessão.
- Worktree isolado (`git worktree`) a partir do clone local — não reclona, não
  toca o working tree/branches existentes.
- `auto_dispatch=false` por padrão — comece avisando, ligue o disparo quando confiar.

## Labels (o vocabulário da esteira)

| Label | Significado |
|---|---|
| `ideia/aguardando-spec` | ideia crua ou em especificação (só humano) |
| `aguardando-liberacao` | spec revisada, na fila aguardando liberação |
| `aguardando-desenvolvimento` | **liberado — a esteira pega** |
| `em-desenvolvimento` | sessão implementando |
| `em-teste` | validando (testes/QA) antes do PR |
| `aguardando-code-review` | PR aberto, esperando revisão humana |
| `acao-necessaria` | travou, precisa de decisão |
| `segurar` | não fazer auto-merge (põe antes) |
| `bloqueado` | travado por dependência |

Aplique-as num repo:
```bash
./scripts/setup-labels.sh owner/repo
```

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
   #   name="esteira-deployment", script="~/.kiro/crew/crons/deployment.py:run", every=600
   ```
3. Aplique as labels nos seus repos (`scripts/setup-labels.sh`).
4. Comece com `auto_dispatch: false` (só avisa). Quando confiar, mude para `true`.

## Fluxo de trabalho

1. Você cria a issue e conversa/especifica (labels `ideia/aguardando-spec` → `aguardando-liberacao`).
2. Quando decidir soltar, marca `aguardando-desenvolvimento`.
3. A esteira detecta e (Fase 1) avisa você, ou (Fase 2) dispara a sessão.
4. A sessão implementa, abre PR e mergeia (a menos que a issue tenha `segurar`).

## Segurança / privacidade

- Nada de dado pessoal no código: repos, chat_id e paths vivem no `config.yaml`
  (gitignored). O `config.example.yaml` só tem placeholders.
- O disparo usa o segredo interno do gateway apenas em loopback (localhost).
