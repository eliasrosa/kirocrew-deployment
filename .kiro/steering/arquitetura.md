---
inclusion: always
name: arquitetura-codigo
description: Arquitetura hexagonal, stack, convenções de código e qualidade do KiroCrew Flow. Ler antes de tocar qualquer arquivo Python do projeto.
---

# Arquitetura do KiroCrew Flow

## Stack

| Componente | Tecnologia | Observação |
|---|---|---|
| Linguagem | **Python 3.12** | |
| Servidor web | **aiohttp** | NÃO usar FastAPI, Flask, Starlette ou Pydantic |
| Testes | **pytest** + **pytest-cov** | 137 testes, 75% de cobertura mínima |
| Linting | **ruff** | zero warnings permitidos |
| Type check | **mypy** | `--ignore-missing-imports` no CI |

## Estrutura hexagonal (obrigatória)

```
flow/
├── domain/       ← NÚCLEO: sem I/O, testável sem mock
│   ├── state.py  — State, Modifier, is_dispatchable(), can_transition()
│   └── gates.py  — Result, WorkItem, Squad, can_leave_spec(), triage_hotfix()...
├── ports/
│   └── issue_provider.py  — IssueProvider (Protocol), provider_for(), PROVIDERS
├── adapters/
│   ├── github_transport.py     — fachada patchável sobre gh CLI
│   ├── github_normalization.py — payload GitHub → contrato canônico
│   ├── github_client.py        — orquestração; satisfaz IssueProvider
│   ├── jira_transport.py       — REST HTTP via stdlib
│   ├── jira_normalization.py   — payload Jira → contrato canônico
│   └── jira_client.py          — orquestração; satisfaz IssueProvider
└── tests/
    ├── test_domain_boundary.py  — garante domain/ isolado (NUNCA viola)
    ├── test_domain_state.py
    ├── test_domain_gates.py
    ├── test_ports_issue_provider.py
    ├── test_adapter_github.py
    ├── test_adapter_jira.py
    └── test_provider_parity.py  — garante que os dois adapters expõem a mesma superfície
```

## Regra de isolamento do domínio (INVIOLÁVEL)

O teste `test_domain_boundary.py` quebra o CI se qualquer arquivo em `flow/domain/`
importar:
- `flow.adapters`
- `aiohttp`, `subprocess`, `socket`, `requests`, `httpx`
- qualquer outra biblioteca de I/O

**Por quê:** o domínio isolado é testável sem mock nenhum — é o argumento central da
arquitetura. Se o teste de fronteira quebrar, pare tudo e conserte antes de avançar.

## Adapters são MÓDULOS, não classes

O dispatch é um dict de módulos:
```python
_PROVIDERS = {"github": github_client, "jira": jira_client}
provider_for("jira")  # → o módulo jira_client
```

Um módulo **não pode** ser verificado estaticamente contra um Protocol. O gate que
garante paridade é `test_provider_parity.py`. Se você adicionar um terceiro adapter,
registre-o também na tabela `CLIENTS` do teste — senão o CI passa sem verificar.

## Convenções de código

### Enums de label

Use `StrEnum` (não `str, Enum`):
```python
class State(StrEnum):
    TODO = "crewflow:todo"
```

### Dataclasses

Sempre `frozen=True, slots=True`:
```python
@dataclass(frozen=True, slots=True)
class WorkItem:
    key: str
    title: str
```

### Injeção de dependência

Resultados de decisões externas (aprovação humana, existência de teste) chegam
**como parâmetros**, nunca são buscados dentro da função:
```python
# CORRETO
def can_start_debt(item, has_tl_approval: bool) -> Result: ...

# ERRADO — não buscar credencial, sessão ou API de dentro do domínio
def can_start_debt(item, jira_client) -> Result: ...
```

### Imports nos adapters

Todos os imports no **topo do arquivo**. Não usar import local dentro de função
exceto quando há circular import genuíno (documentar o motivo).

### `with` múltiplos

Preferir um único `with` com múltiplos contextos:
```python
# CORRETO
with mock.patch.object(transport, "fn"), pytest.raises(Error):
    ...

# EVITAR
with mock.patch.object(transport, "fn"):
    with pytest.raises(Error):
        ...
```

## Adicionar um novo provedor (além de GitHub e Jira)

1. Criar `flow/adapters/<nome>_transport.py`, `_normalization.py` e `_client.py`
2. Adicionar `"<nome>"` em `PROVIDERS` em `flow/ports/issue_provider.py`
3. Adicionar ao dispatch em `_build_dispatch()`
4. **Obrigatório:** adicionar à tabela `CLIENTS` em `test_provider_parity.py`
5. O `test_a_tabela_cobre_todos_os_providers_registrados` quebra até o passo 4 ser feito

## Rodar o CI localmente

```bash
# Lint
python3 -m ruff check flow/

# Type check
python3 -m mypy flow/ --ignore-missing-imports

# Testes + cobertura
python3 -m pytest flow/tests/ --cov=flow --cov-report=term-missing

# Tudo junto
python3 -m ruff check flow/ && python3 -m mypy flow/ --ignore-missing-imports && python3 -m pytest flow/tests/ --cov=flow --cov-fail-under=75
```

## Notas de implementação

- `list_changed_since()` nos dois adapters é ingênuo na Fase 1: varre todos os
  estados para detectar mudança. Dívida técnica documentada — a fix está na #6.
- Os adapters são síncronos. `aiohttp` é async-first, então a migração para
  `async def` vai ser necessária quando o scan for integrado ao cron. Os transports
  já estão estruturados para absorver isso sem mudar a interface.
- `deployment/deployment.py` é o **driving adapter** da Fase 1 — ele é o cron que
  chama o domínio. Quando a arquitetura hexagonal estiver completa, a lógica de
  dispatch do `deployment.py` migrará para consumir `provider_for()` e as regras
  de `domain/`.
