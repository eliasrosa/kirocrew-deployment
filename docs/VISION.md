# KiroCrew Flow — Visão do Produto

## O problema

Squads de engenharia gastam horas por semana gerenciando o estado do trabalho:
quem está em qual tarefa, o que está bloqueado, quando um PR está pronto para
review, qual issue passou para QA. Esse acompanhamento é manual, propenso a
erro e, na prática, ninguém faz completamente.

O resultado: PMs com status desatualizado, PRs esquecidos em review, bugs que
voltam de QA sem registro, e um tech lead que precisa ser "o sistema" que rastreia
tudo — tempo que deveria ir para decisões técnicas.

## A solução

**KiroCrew Flow** é um sistema de orquestração de esteira de desenvolvimento
multi-squad sobre o Kiro Crew. O estado de cada tarefa vive na própria issue
(via labels `crewflow:*`), o scan é zero-token, e o motor nunca mergeia nem
faz deploy — tudo isso é humano.

**A issue É o estado.** Não há banco de trabalho paralelo. Label = estado atual,
comentário estruturado = histórico auditável. Distribuído nativamente e visível
para todo o time em qualquer ferramenta.

## Princípios

1. **Zero-token no polling.** O scan é Python puro — não acorda o agente. Token
   só é gasto quando há trabalho real.

2. **Merge é SEMPRE manual.** A automação abre o PR e para em `flow:review-waiting`.
   Nenhum merge, nenhum deploy é automatizado.

3. **Gates humanos são invioláveis.** Aprovação de spec, de code review e de QA
   são sempre de pessoas. O GATE 1 (TL aprova review) não pode ser pulado mesmo
   em hotfix.

4. **Exceções são auditáveis, não invisíveis.** O bypass do HML (hotfix direto
   pra PRD) existe, acontece, e o motor o rastreia — com justificativa obrigatória
   e `flow:blocked` (via auditoria no comentário de estado) para que a frequência
   seja mensurável.

5. **Template por squad.** A Cogna usa a Versão C (review antes do QA). Outra
   squad pode usar Versão A (review depois do QA). O motor executa qualquer grafo
   válido — só muda o template.

## Modelo de labels

Duas dimensões independentes:

| Dimensão | Cardinalidade | Exemplos |
|---|---|---|
| **Estado** (`flow:*`) | exatamente 1 | `flow:develop-waiting`, `flow:review-waiting`, `flow:done` |
| **Modificador** (`flow:*`) | 0..N, sobrepõem | `flow:blocked`, `flow:merge-conflict`, `flow:reviewed` |
| **Metadado** (`crewflow:*`) | 0..N | `crewflow:feature`, `crewflow:p1`, `crewflow:blocked` |

> Labels de estado legadas (`crewflow:todo`, `crewflow:review`, etc.) foram deprecadas.
> Use `setup-flow-labels.sh` em novos repos.

Modificador de parada (`flow:blocked`) tem prioridade sobre qualquer estado.

## Fluxos implementados (Fase 1)

| Template | Quando usar |
|---|---|
| `feature` (Versão C) | Feature nova. Review ANTES do QA, sequencial. |
| `bug` | Correção de bug. Mesma ordem da Versão C. |
| `hotfix` | Incidente em PRD. GATE 0 filtra o que é realmente urgente. |
| `debt` | Refatoração sem mudança de comportamento. TL aprova a entrada; QA valida equivalência. |

Diagramas detalhados em [`docs/diagramas/README.md`](diagramas/README.md).

## O que NÃO é este produto

- **Não é um CI/CD.** Não faz build, não faz deploy, não roda testes. Integra com
  os sistemas existentes.
- **Não é um substituto do Jira/GitHub.** Trabalha em cima deles, usando as labels
  como contrato.
- **Não é autônomo.** O motor dispara sessões one-shot do Kiro Crew. Depende do
  Kiro Crew rodando.
