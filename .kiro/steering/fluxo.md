---
inclusion: always
name: fluxo-esteira
description: Fluxo de desenvolvimento da esteira kirocrew-deployment — do rascunho da ideia ao merge, governado por labels, com um motor de execução one-shot sobre o Kiro Crew.
---

# Fluxo da esteira (kirocrew-deployment)

Esteira de desenvolvimento autônoma sobre o **Kiro Crew**. Uma vigia zero-token
observa issues por label e, quando uma issue está liberada, dispara uma **sessão
de execução one-shot** que implementa, abre PR e (se permitido) mergeia — uma
passada, sem loop.

## Fluxograma

```mermaid
flowchart TD
    A["💡 ideia/aguardando-spec<br/>(rascunho ou especificando)"] -->|spec fechada| B["🟡 aguardando-liberacao<br/>(na fila)"]
    B -->|humano libera| C["🟢 aguardando-desenvolvimento<br/>(a esteira pega)"]
    C -->|vigia dispara sessão one-shot| D["🔵 em-desenvolvimento<br/>(implementando)"]
    D --> T["🔷 em-teste<br/>(validando testes/QA)"]
    T --> E["🔀 PR aberto"]
    E --> F{"tem label<br/>segurar?"}
    F -->|não| G["🟢 auto-merge<br/>(squash quando verde)"]
    F -->|sim| H["🟣 aguardando-code-review<br/>(humano revisa/mergeia)"]
    G --> I["✔️ resolvido"]
    H -->|humano aprova| I
    D -.->|precisa de decisão| J["🟠 acao-necessaria<br/>(volta pro humano)"]
    T -.->|teste falhou| J
    D -.->|dependência| K["⚫ bloqueado"]
    J -.->|decidido| C

    classDef human fill:#fde68a,stroke:#b45309,color:#000
    classDef auto fill:#bbf7d0,stroke:#15803d,color:#000
    class A,B,H,J human
    class C,D,T,E,G,I auto
```

## Labels (o vocabulário do fluxo)

| Label | Cor | Significado | Quem mexe |
|---|---|---|---|
| `ideia/aguardando-spec` | 🟡 `#FEF3C7` | ideia crua ou em especificação | 🔒 humano |
| `aguardando-liberacao` | 🟨 `#FBBF24` | spec pronta, na fila | 🧠 humano libera |
| `aguardando-desenvolvimento` | 🟢 `#16A34A` | **gatilho — a esteira pega** | 🤖 esteira |
| `em-desenvolvimento` | 🔵 `#2563EB` | sessão implementando | 🤖 esteira |
| `em-teste` | 🔷 `#0EA5E9` | validando (testes/QA) antes do PR | 🤖 esteira |
| `aguardando-code-review` | 🟣 `#8B5CF6` | PR aberto, esperando revisão | 🧠 humano |
| `acao-necessaria` | 🟠 `#F97316` | travou, precisa de decisão | 🤖→🧠 |
| `segurar` | 🔴 `#DC2626` | não fazer auto-merge (põe antes) | 🧠 humano |
| `bloqueado` | ⚫ `#374151` | travado por dependência | — |

**Gatilho único:** só `aguardando-desenvolvimento` faz a esteira agir. Tudo antes
dela é humano; o resto é a sessão.

## Como o motor dispara (sem loop, sem sandbox)

1. Cron de **script** (zero token) varre os repos por `aguardando-desenvolvimento`
   (ignora quem tem `em-desenvolvimento`).
2. Se `auto_dispatch=true` e há vaga (respeitando `max_concurrent` e 1-por-repo),
   faz um POST loopback interno pra `/api/chat` — cria uma sessão one-shot que roda
   o turno **in-process no gateway** (sem sandbox, sem watchdog).
3. A sessão usa um **worktree isolado** do clone local (não reclona, não bagunça),
   implementa, abre PR, mergeia (ou para em `segurar`/`acao-necessaria`), limpa as
   labels e **encerra**. Uma passada.

## Travas de segurança

- `auto_dispatch=false` por padrão (só avisa até você confiar).
- `max_concurrent` (default 2) e **1 sessão por repo** (evita colisão de merge).
- `max_turns_per_task` — teto duro por sessão.
- Worktree isolado + ordem de nunca tocar outros worktrees/branches.

> Depende do Kiro Crew rodando — é uma receita/plugin, não um app standalone.
