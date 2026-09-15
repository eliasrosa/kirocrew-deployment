# Diagramas dos fluxos — KiroCrew Flow

Os **templates fixos da Fase 1**. Cada diagrama É a máquina de estados que o
motor executa: nó = estado, aresta = transição, losango = gate.

Fonte Mermaid em `resources/mermaid/*.mmd` (a fonte é a verdade; o PNG é
derivado). Para regerar um PNG:

```bash
npx @mermaid-js/mermaid-cli \
  -i resources/mermaid/<nome>.mmd \
  -o docs/diagramas/<nome>.png \
  --backgroundColor white
```

> **Ambiente sem sandbox do Chromium** (containers, algumas distros): o
> `mermaid-cli` falha com `No usable sandbox!`. Crie um config local — não é
> versionado de propósito, porque desabilitar o sandbox é decisão do seu
> ambiente, não do projeto:
>
> ```bash
> echo '{"args":["--no-sandbox","--disable-setuid-sandbox"]}' > docs/diagramas/.puppeteer.json
> ```
>
> e acrescente `-p docs/diagramas/.puppeteer.json` ao comando acima.

## Os quatro fluxos

| Fluxo | Fonte | PNG | Tipo |
|-------|-------|-----|------|
| **Feature (Versão C)** — oficial | [`fluxo-feature-versao-c.mmd`](../../resources/mermaid/fluxo-feature-versao-c.mmd) | [PNG](fluxo-feature-versao-c.png) | `sequenceDiagram` |
| **Bug** | [`fluxo-bug.mmd`](../../resources/mermaid/fluxo-bug.mmd) | [PNG](fluxo-bug.png) | `sequenceDiagram` |
| **Hotfix / incidente PRD** | [`fluxo-hotfix.mmd`](../../resources/mermaid/fluxo-hotfix.mmd) | [PNG](fluxo-hotfix.png) | `flowchart` |
| **Débito técnico** | [`fluxo-debito-tecnico.mmd`](../../resources/mermaid/fluxo-debito-tecnico.mmd) | [PNG](fluxo-debito-tecnico.png) | `flowchart` |

### Feature — Versão C (oficial)

Code review **ANTES** do QA, sequencial:

```
Dev → PR → 🤖 review → TL aprova → deploy HML manual → QA → merge manual
```

As Versões A (review depois do QA) e B (review em paralelo) foram descartadas
em 14/09 e viraram nota histórica.

### Bug

Mesma ordem da Versão C, com investigação automática shift-left na entrada.

### Hotfix / incidente PRD

**Não é um gitflow diferente — é a Versão C comprimida.** A squad não tem branch
`hotfix/*`: `release/VGAT-XXX` sai da `main`, e merge na `main` dispara PRD.

O que muda é o que pode ser encurtado:

| Etapa | No hotfix |
|-------|-----------|
| `spec` / `ready` / `todo` | pulados — o incidente é a spec, `p1` é a priorização |
| 🤖 code review | **mantido** — custa minutos, e a pressa é quando o erro é mais provável |
| GATE 1 (TL aprova) | **inviolável** |
| Deploy HML | pulável como **exceção auditada** (`crewflow:hml-bypass` + justificativa) |
| QA | reduzido a smoke do incidente |
| Merge | **manual** |
| Post-mortem | **obrigatório** → abre um `crewflow:debt` |

O `GATE 0` na entrada existe pro vício clássico: todo mundo acha que o próprio
bug é hotfix. Se não é PRD quebrada, é rebaixado pra `crewflow:bug`.

### Débito técnico

O que inverte este fluxo: **não tem PM e não tem validação funcional** — o
comportamento não deveria mudar. Então o **TL** aprova a entrada (justificativa
técnica, não de produto), e o QA **prova que nada mudou** (regressão) em vez de
validar comportamento novo.

O nó central é `COV`: existe teste que prova equivalência? Refatorar sem isso é
o jeito mais comum de transformar dívida em incidente. Se não existe, o primeiro
trabalho é escrever o teste **contra o código antigo**, pra ele passar antes e
depois.

## Ligação entre os fluxos

Todo hotfix termina abrindo um `crewflow:debt` com o que foi encurtado — a
regressão que não rodou, o teste que faltou. É o que impede a pressa de virar
dívida invisível.
