# KiroCrew Flow — Arquitetura

## Stack

| Item | Valor |
|---|---|
| Linguagem | Python 3.12 |
| Servidor web | aiohttp |
| Testes | pytest + pytest-cov |
| Linting | ruff |
| Type check | mypy |
| Cache do scan | SQLite (stdlib) |
| YAML | PyYAML com fallback para parser interno |

Sem FastAPI, Flask, Starlette ou Pydantic — o projeto segue a stack do próprio
Kiro Crew, validada lendo o código instalado.

## Padrão arquitetural: hexagonal (ports & adapters)

O motivo prático: a regra de negócio mais importante do fluxo (`can_leave_spec`)
fica testável **sem nenhuma infraestrutura** — nenhuma rede, nenhum banco, nenhum
mock de provider.

```
flow/
├── domain/    ← NÚCLEO: sem I/O, testável sem mock
├── ports/     ← contratos (Protocol)
├── adapters/  ← GitHub e Jira
├── scan/      ← zero-token polling
├── executor/  ← decisões por template
├── audit/     ← comentário de auditoria
└── config/    ← squad config + workflow templates
```

## Regra de isolamento do domínio

`test_domain_boundary.py` verifica por AST que nenhum arquivo em `flow/domain/`
importa infraestrutura. Se quebrar, pare tudo e conserte primeiro.

## Adapters são módulos, não classes

```python
_PROVIDERS = {"github": github_client, "jira": jira_client}
provider_for("jira")  # → o módulo jira_client
```

Um módulo não pode ser verificado estaticamente contra um `Protocol`. O gate que
garante conformidade é `test_provider_parity.py` — compara GitHub (referência)
contra Jira, com um auto-guard que falha se um terceiro adapter entrar no dispatch
sem ser registrado no teste.

Cada adapter tem três camadas:

| Camada | Papel |
|---|---|
| `*_transport.py` | I/O bruto — fachada patchável nos testes |
| `*_normalization.py` | payload do provedor → contrato canônico |
| `*_client.py` | orquestração; satisfaz `IssueProvider` |

## Identidade: projeto + issue key

A chave primária de um item de trabalho é **projeto + issue key** (`VGAT-123`).
O repo é derivado do título (`[repo] Descrição`), não da identidade.

O Jira é projeto-cêntrico por natureza. O GitHub é repo-cêntrico, mas a porta
normaliza para projeto para que o domínio seja agnóstico de provedor.

## Fluxo de uma issue pelo sistema

```
squads/my-squad.yaml
    → SquadConfig.resolve_workflow(labels)  → "hotfix-flow"
    → scan_candidates(config, provider, conn)  → [ScanResult]
    → executor.decide(result, state_comment)   → ExecutorDecision
    → provider.set_labels() + upsert_state_comment() + _dispatch()
```

O `deployment.py` é o **driving adapter** que executa esse loop como cron de
script do Kiro Crew (zero token no polling).

## Workspace isolado por task (Fase 2)

Cada dispatch cria um **worktree efêmero** dedicado, garantindo que múltiplas
tasks rodem em paralelo sem pisar uma na outra.

### Convenção de caminhos

```
<dev_root>/<repo-short>/          ← clone-base (nunca tocado diretamente)
<dev_root>/.esteira-worktrees/
    <repo-short>-<issue_number>/  ← worktree efêmero (criado no dispatch, removido no fim)
```

A função `_worktree_path(dev_root, repo, issue_number)` é a **fonte única de
verdade** do caminho: usada pelo `deployment.py` na limpeza pré-dispatch e pelo
prompt enviado à sessão one-shot. Os dois lados sempre falam do mesmo diretório.

### Fluxo pré-dispatch

```
scan_candidates()
    → executor.decide()
    → _resource_headroom_ok()   ← posture critical suspende dispatch
    → _clean_stale_worktree()   ← remove worktree órfão de sessão anterior
    → _dispatch()               ← prompt inclui git worktree add no caminho canônico
```

### Cap de concorrência

`max_concurrent_tasks` (alias de `max_concurrent` para compat) limita sessões ativas
simultaneamente. O headroom de recursos é verificado via `resource_status` do Kiro
Crew antes de cada dispatch — posture `critical` adia a task sem bloquear o ciclo.

| Campo config | Função | Default |
|---|---|---|
| `max_concurrent_tasks` | Cap global de tasks em paralelo | `2` |
| `one_per_repo` | No máx 1 sessão ativa por repo | `true` |

## Identificação e labels

O prefixo `crewflow:` funciona em Jira e GitHub. Confluence rejeita `:` — fora
de escopo.

Estados (1 por vez):
```
crewflow:spec → crewflow:ready → crewflow:todo → crewflow:dev →
crewflow:review → crewflow:qa → crewflow:done
```

Modificadores (0..N):
```
crewflow:blocked   # para tudo (prioridade sobre o estado)
crewflow:running   # trabalho em andamento
crewflow:reviewed  # lock anti-loop de code review
crewflow:hml-bypass  # bypass auditado do HML
```

## O comentário de estado

```markdown
<!-- KIRO-FLOW-STATE -->
## 🤖 KiroCrew Flow — Estado

**Workflow:** feature (v1)
**Nó atual:** review
**Status:** running
**Repo:** api-gateway2

### Histórico
| Quando | De → Para | Quem |
|--------|-----------|------|
| 2026-09-15 00:02 | start → dev | system |

### Exceções
| Exceção | Justificativa | Quem | Quando |
|---------|---------------|------|--------|
| `crewflow:hml-bypass` | Checkout fora do ar | @elias | 2026-09-15 |
<!-- /KIRO-FLOW-STATE -->
```

O comentário não é só registro — é **fonte de pré-condições de merge**. O executor
bloqueia o merge com `crewflow:hml-bypass` se a seção de Exceções não tiver
justificativa preenchida.

## Detalhes técnicos: para desenvolvedores

Ver `.kiro/steering/arquitetura.md` — cobre convenções de código (StrEnum,
`slots=True`, imports no topo, `with` múltiplos), como adicionar um novo
provedor, e como rodar o CI localmente.
