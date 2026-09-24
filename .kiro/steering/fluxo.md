---
inclusion: always
name: fluxo-esteira
description: Fluxo de desenvolvimento do KiroCrew Flow — da especificação ao merge, governado por labels flow:*, com motor de execução one-shot sobre o Kiro Crew. Merge sempre manual.
---

# Fluxo da esteira (KiroCrew Flow)

Orquestração de esteira de desenvolvimento sobre o **Kiro Crew**. Uma vigia
zero-token observa issues por label `flow:*` e, quando uma task está
priorizada, dispara uma **sessão de execução one-shot** que implementa e **abre o
PR** — uma passada, sem loop.

> **Regra inviolável: a automação NUNCA faz deploy.** Ela entrega o PR no estado
> `flow:review-waiting` e encerra (ou faz merge squash se `auto_merge_on_approve: true`
> estiver configurado no squad config). Deploy é sempre manual.
> Merge é **manual por padrão** (`auto_merge_on_approve: false`). Ative por squad
> config para habilitar merge squash automático após approve sem comentários.

## Fluxo completo (namespace flow:*)

```
flow:briefing → flow:planning-specs → flow:planning-review → flow:develop-waiting
  → flow:develop-running → flow:review-waiting
    → flow:review-approved → flow:qa-waiting → flow:qa-testing
      → flow:qa-approved → flow:done
      → flow:qa-refused → (gate humano: TL/dev move p/ develop-waiting|develop-running)
    → flow:review-refused → (gate humano: TL/dev move p/ develop-waiting)
```

Modificadores: `flow:blocked`, `flow:merge-conflict`.

**Regra chave:** toda reprovação humana (`review-refused`, `qa-refused`) é gate humano
— o fluxo para e aguarda decisão manual. Sem dispatch automático após reprovação.

## Labels — duas dimensões

O modelo é **estado × modificador**. Um estado por vez; zero ou mais modificadores
sobrepostos. **Modificador de parada (`flow:blocked`) tem prioridade sobre o estado.**

### Estados (1 por vez, ordem canônica)

| Label | Significado | Quem age | Automatizado |
|---|---|---|---|
| `flow:briefing` | TL/PM criou demanda + briefing | 🧠 humano | Não |
| `flow:planning-specs` | Dev montando spec/critérios/sub-tasks | 🧠 dev | Parcial |
| `flow:planning-review` | Dev pediu revisão ao TL/PM | 🧠 TL/PM | Não |
| `flow:develop-waiting` | Aguardando agente pegar — **GATILHO** | 🤖 | Sim |
| `flow:develop-running` | Agente implementando | 🤖 | Sim |
| `flow:review-waiting` | PR aberta, aguardando reviewer | 🤖 | Sim |
| `flow:review-approved` | Reviewer aprovou | 🤖 | Sim |
| `flow:review-refused` | Reviewer reprovou — **gate humano** | 🧠 | Não |
| `flow:qa-waiting` | Aguardando QA | 🧠 | Não |
| `flow:qa-testing` | QA testando | 🧠 | Não |
| `flow:qa-approved` | QA aprovou — **gatilho merge** | 🤖 | Sim |
| `flow:qa-refused` | QA reprovou — **gate humano** | 🧠 | Não |
| `flow:done` | Concluído | — | Sim |

### Modificadores (0..N, sobrepõem)

| Label | Significado |
|---|---|
| `flow:blocked` | Bloqueado — **para tudo** (prioridade sobre o estado) |
| `flow:merge-conflict` | PR com conflito de merge ou base desatualizada — cron resolve via rebase |

**Gatilho único:** só `flow:develop-waiting` faz a esteira agir. Tudo antes dela é humano;
tudo depois do PR também.

## Como o motor dispara (arquitetura hexagonal)

O loop completo:

```
squads/*.yaml
    → SquadConfig.resolve_workflow(labels)  → template (feature/bug/hotfix/debt)
    → scan_candidates(config, provider, conn)  → zero token, SQLite cache
    → executor.decide(result, state_comment, squad)  → puro Python, sem I/O
    → deployment.run() executa a decisão:
        DISPATCH_DEV       → sessão one-shot (implementa + abre PR)
        DISPATCH_REVIEWER  → notifica que kiro-reviewer foi disparado
        DISPATCH_REWORK    → sessão dev de re-trabalho pós-rework humano (não automático)
        DISPATCH_CONFLICT_RESOLVER → sessão de resolução de conflito (rebase na branch feat/issue-N)
        MARK_CONFLITO      → aplica flow:merge-conflict na issue (PR com mergeable=CONFLICTING)
        NOTIFY_HUMAN       → avisa TL / Dev / QA pelo papel correto (gates humanos)
        BLOCK              → notifica bypass sem justificativa
        REBRAND            → atualiza labels (GATE 0 do hotfix)
        SKIP               → silêncio
```

### Crons por estágio

| Entrypoint | Estado alvo | Ação | Intervalo recomendado |
|---|---|---|---|
| `run_dev` | `flow:develop-waiting` | `DISPATCH_DEV` — implementa + abre PR | 600s (10 min) |
| `review_waiting.py:run` | `flow:review-waiting` (sem `flow:review-running`) | `DISPATCH_REVIEWER` — code review | 300s (5 min) |
| `run_review_approved` | `flow:review-approved` | `MERGE_PR` → `flow:qa-waiting` | 120s (2 min) |
| `run_qa_approved` | `flow:qa-approved` | `MERGE_PR` → `flow:done` | 120s (2 min) |
| `run_merge` | `flow:review-approved` ou `flow:qa-approved` | `MERGE_PR` — ambos (depreciado) | 120s (2 min) |
| `run_conflito` | `flow:merge-conflict` | `DISPATCH_CONFLICT_RESOLVER` | 300s (5 min) |

## Lock anti-loop: `flow:review-running`

Uma análise por SHA. O robô de review adiciona `flow:review-running` ao iniciar a análise
(lock interno). Quando o review termina:
- Aprovado → move para `flow:review-approved` (remove `flow:review-waiting,flow:review-running`)
- Reprovado → move para `flow:review-refused` (gate humano — não redespacha automaticamente)

Quando o dev faz novo push, `flow:review-running` é invalidado (novo SHA) e a próxima varredura
dispara nova análise.

## Travas de segurança

- `auto_dispatch=false` por padrão (só avisa até você confiar).
- `max_concurrent_tasks` (default 2) — cap por número de issues em `flow:develop-running` no scan.
- `max_turns_per_task` — teto duro por sessão.
- Worktree isolado + ordem de nunca tocar outros worktrees/branches.
- **Nenhum deploy automatizado** — trava de produto, não de config.
- **Gates humanos invioláveis:** `review-refused` e `qa-refused` NUNCA despacham automaticamente.
  Apenas notificam o TL e aguardam decisão manual.

## Migração crewflow:* → flow:*

| crewflow | flow |
|---|---|
| `crewflow:spec` | `flow:briefing` |
| `crewflow:ready` | `flow:planning-specs` |
| `crewflow:todo` | `flow:develop-waiting` |
| `crewflow:dev` | `flow:develop-running` |
| `crewflow:review` | `flow:review-waiting` |
| `crewflow:review-ok` | `flow:review-approved` |
| `crewflow:review-fail` | `flow:review-refused` |
| `crewflow:qa` | `flow:qa-waiting` |
| `crewflow:done` | `flow:done` |
| `crewflow:blocked` | `flow:blocked` |
| `crewflow:conflito` | `flow:merge-conflict` |
| `crewflow:reviewed` | `flow:review-running` (lock interno) |
| `crewflow:running` | eliminado (absorvido em `flow:develop-running`) |
| `crewflow:changes-requested` | eliminado (substituído por `flow:review-refused`) |

Use `scripts/setup-flow-labels.sh` para criar labels no novo namespace e
`scripts/migrate-labels.sh` para migrar issues existentes.

> Depende do Kiro Crew rodando — é uma receita/plugin, não um app standalone.
