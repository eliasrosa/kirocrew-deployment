# KiroCrew Flow — Roadmap

## Fases

| Fase | Objetivo | Estado |
|---|---|---|
| **Fase 1** | Fluxos fixos, sem editor | ✅ Concluída (set/2026) |
| **Fase 2** | Editor read-only — visualizar o grafo no dashboard | 🔲 Planejada |
| **Fase 3** | Editor editável — canvas estilo n8n, arrastar e conectar | 🔲 Futura |

O comportamento não muda entre as fases — só muda de "hardcoded" (Fase 1) para
"editável no canvas" (Fase 3).

---

## Fase 1 — Concluída ✅

### Implementado

| Componente | Módulo | Issues |
|---|---|---|
| Domínio: estado × modificador | `flow/domain/state.py` | #14, #15 |
| Domínio: regras dos gates | `flow/domain/gates.py` | #14, #16 |
| Porta (Protocol) | `flow/ports/issue_provider.py` | #17 |
| Adapter GitHub | `flow/adapters/github_*.py` | #18 |
| Adapter Jira | `flow/adapters/jira_*.py` | #19 |
| Teste de paridade | `test_provider_parity.py` | #20 |
| Scan zero-token + cache SQLite | `flow/scan/` | #6 |
| Executor de decisões por template | `flow/executor/` | #7 |
| Comentários de auditoria | `flow/audit/` | #8 |
| Squad config + routing | `flow/config/squad.py` | #3, #5 |
| Workflow templates (4) | `flow/config/workflow.py` | #4 |
| CI, ruff, mypy, cobertura | `pyproject.toml`, `ci.yml` | — |
| Diagramas dos 4 fluxos | `resources/mermaid/`, `docs/diagramas/` | #13 |

### Arquitetura hexagonal (242 testes, 82% cobertura, ruff limpo)

Destaques arquiteturais da Fase 1:

- **Domínio isolado por teste**: `test_domain_boundary.py` falha no CI se qualquer
  arquivo em `flow/domain/` importar infraestrutura.
- **Paridade de adapters por teste**: `test_provider_parity.py` compara GitHub
  (referência) contra Jira; auto-guard impede que um terceiro adapter entre
  no dispatch sem ser verificado.
- **Executor puro**: `executor.decide()` recebe um `ScanResult` e retorna um
  `ExecutorDecision` sem nenhum I/O — totalmente testável sem mock.

### O que a Fase 1 NÃO entrega

- Auto-merge e auto-deploy (futuro, opcional por squad)
- Editor visual de fluxos (Fase 3)
- Conexão fim-a-fim `squad.yaml → scan → executor → dispatch` (o driving adapter
  `deployment.py` ainda usa a config old-style do `deployment.config.yaml`)
- Investigação shift-left para bugs (nó de investigação antes do dev)
- Fluxos de spike e enabler

---

## Fase 2 — Planejada 🔲

**Objetivo:** visualizar o grafo no dashboard sem editar.

- Página no dashboard (app Kiro Crew) mostrando o grafo do workflow
- Timeline de eventos por issue
- Métricas: cycle time, issues por estado, frequência de bypass
- Editor read-only (não salva, só mostra)

**Pré-requisito técnico:** React via import map (sem build step — ver notas em
`.kiro/steering/arquitetura.md`). Atenção: React Flow (biblioteca de canvas)
assume bundler + JSX; sem build step o canvas precisaria de outra abordagem.
Validar antes de fechar o design da Fase 3.

---

## Fase 3 — Futura 🔲

**Objetivo:** editor de workflows editável — canvas estilo n8n.

- Arrastar nós, conectar arestas, salvar por squad
- `squads/*.yaml` gerenciado pela UI, não manualmente
- Catálogo de nós expandível (hoje fixo: PM/TL/Dev/QA + kiro-reviewer/kiro-dev)
- Auto-merge opcional por squad (quando ligar: `merge_mode: auto` no workflow_params)

---

## Dívida técnica documentada

| Item | Módulo | Impacto |
|---|---|---|
| `list_changed_since()` ingênuo | `github_client`, `jira_client` | Escaneia todos os estados a cada ciclo; ok para squad pequena |
| Adapters síncronos | `*_transport.py` | Precisará de `async def` quando integrado ao servidor web (aiohttp) |
| `_detect_template()` duplicado | `executor.py` | Mesma lógica de `SquadConfig.resolve_workflow()` — unificar |
| `deployment.py` com config old-style | `deployment.py` | Não usa `load_squad()` ainda; usa `SquadScanConfig` provisório |
| Fluxo de bug sem shift-left | `bug-flow` template | O executor trata bug igual a feature na Fase 1 |

---

## Notas de release

### set/2026 — Fase 1

- Projeto renomeado de `kirocrew-deployment` para `kirocrew-flow`
- 18 labels `crewflow:*` criadas; labels legadas removidas
- Motor que nunca mergeia (PR #10)
- Arquitetura hexagonal completa (PRs #14–#20)
- Scan zero-token com cache SQLite (PR #31)
- Executor declarativo por template (PR #33)
- Comentários de auditoria (PR #34)
- Squad config + routing + workflow templates (PR #35)
- CI com ruff + mypy + coverage (PR #30)
- 4 fluxos desenhados e versionados — feature, bug, hotfix, débito técnico (PR #13)
