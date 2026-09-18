# kirocrew-flow

**KiroCrew Flow** — orquestração de esteira de desenvolvimento multi-squad sobre o
**[Kiro Crew](https://github.com/kirodotdev)**: os fluxos de trabalho são **grafos**,
o estado de cada task vive em **labels `crewflow:*`** na própria issue, e o polling
é **zero-token**.

> ⚠️ **Não é standalone.** Depende do Kiro Crew rodando na máquina: usa o loopback
> interno do gateway (`POST /api/chat`) e o formato de *cron de script* do Kiro Crew.

## Princípios

1. **A issue É o estado.** Label = estado atual, comentário = histórico auditável.
2. **Zero-token no polling.** O scan é Python puro — token só gasto quando há trabalho real.
3. **Merge é SEMPRE manual.** A automação abre o PR e para. Nenhum merge, nenhum deploy automatizado.
4. **Gates humanos são invioláveis.** Aprovação de spec, review e QA são sempre de pessoas.
5. **Exceções são auditáveis.** O bypass do HML (hotfix direto pra PRD) exige justificativa e é rastreado.

## Modelo de labels

Duas dimensões independentes.

### Estados — 1 por vez, nesta ordem

```
crewflow:spec → crewflow:ready → crewflow:todo → crewflow:dev → crewflow:review → crewflow:qa → crewflow:done
```

### Modificadores — 0..N, sobrepõem ao estado

| Label | Significado |
|---|---|
| `crewflow:blocked` | Para tudo (prioridade sobre o estado) |
| `crewflow:running` | Trabalho em andamento |
| `crewflow:reviewed` | Lock anti-loop: já analisado neste SHA |
| `crewflow:hml-bypass` | Exceção auditada: hotfix pulou o HML (exige justificativa) |

### Tipo de fluxo (routing) e prioridade

`crewflow:feature` · `crewflow:bug` · `crewflow:hotfix` · `crewflow:debt`  
`crewflow:p1` · `crewflow:p2` · `crewflow:p3`

Aplique todas num repo:
```bash
./scripts/setup-labels.sh owner/repo
```

## Como funciona

```
squads/*.yaml → SquadConfig → scan_candidates() → executor.decide() → deployment.run()
```

1. `scan_candidates()` varre as issues por labels `crewflow:*` sem gastar token — compara hash do estado atual com o cache SQLite, e só processa o que mudou.
2. `executor.decide()` decide a ação (DISPATCH_DEV, DISPATCH_REVIEWER, NOTIFY_HUMAN, BLOCK, REBRAND ou SKIP) com base no template da squad e no estado da issue.
3. `deployment.run()` executa a ação: dispara sessão one-shot, notifica humano ou aplica rebrand de template.

A sessão one-shot **nunca mergeia e nunca faz deploy**. Ela entrega o PR em `crewflow:review` e encerra.

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

# O script copia deployment.py, aplica o patch de sys.path e
# copia deployment.config.yaml (se não existir).
# Edite ~/.kiro/crew/crons/deployment.config.yaml com seus paths.
```

### 3. Aplique as labels

```bash
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
└── config/      ← SquadConfig + workflow templates

deployment/      ← cron de script do Kiro Crew (driving adapter)
squads/          ← configurações de squad (*.yaml)
workflows/       ← templates de workflow (*.yaml)
resources/mermaid/ ← fonte dos diagramas (.mmd)
docs/            ← VISION.md, ARCHITECTURE.md, ROADMAP.md, diagramas/
```

Ver `.kiro/steering/arquitetura.md` para convenções de código e como adicionar um novo provedor.

## Desenvolvimento

```bash
# Lint
python3 -m ruff check flow/

# Testes + cobertura
python3 -m pytest flow/tests/ --cov=flow --cov-report=term-missing

# Tudo junto
python3 -m ruff check flow/ && python3 -m pytest flow/tests/ --cov=flow --cov-fail-under=75
```

260 testes, 82% cobertura, ruff limpo (Fase 1).

## Roadmap

| Fase | Estado |
|---|---|
| **Fase 1** — fluxos fixos, sem editor | ✅ Concluída (set/2026) |
| **Fase 2** — editor read-only no dashboard | 🔲 Planejada |
| **Fase 3** — canvas editável estilo n8n | 🔲 Futura |

Ver [`docs/ROADMAP.md`](docs/ROADMAP.md) para detalhes.

## Segurança / privacidade

- Repos, chat_id e paths vivem no `config.yaml` (gitignored). O `config.example.yaml` só tem placeholders.
- O disparo usa o segredo interno do gateway apenas em loopback (localhost).
