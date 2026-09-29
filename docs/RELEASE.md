# KiroCrew Flow — Estratégia de Release

> Documento de decisão da issue #247 (item 4). Precisa existir **antes** da
> implementação da pipeline: as features seguintes (configuração do
> `python-semantic-release`, tags de canal e substituição do cron de auto-update)
> implementam exatamente a política descrita aqui. Se algum detalhe concreto de
> comando/wiring ainda estiver em aberto, este documento define a **intenção e a
> política**; a fiação exata fica nas features de implementação, sem contradizê-las.

Este documento cobre as quatro áreas da issue #247:

1. Versionamento semântico (tool + estratégia).
2. Tags de canal (`latest` vs `stable`, com `app.json` apontando para `stable`).
3. Substituição do cron `flow-auto-update` pelo hook oficial de update do Crew App.
4. Decisão auto vs manual (o núcleo do item 4).

Toda a política deste documento é governada pelo **princípio inviolável do projeto**
(ver `.kiro/steering/fluxo.md` e a seção *Princípios* do `README.md`):

> A automação **NUNCA faz deploy** e o **merge é manual por padrão**. Deploy é
> sempre manual; a produção jamais pode ser quebrada automaticamente. Gates
> humanos são invioláveis.

---

## Versionamento semântico

As releases são dirigidas pelos **conventional commits** que o repositório já usa
hoje (basta olhar o `git log`: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`,
`test:`). O bump de versão é derivado automaticamente do tipo de commit desde a
última release:

| Tipo de commit | Exemplo | Bump | Resultado (a partir de `X.Y.Z`) |
|---|---|---|---|
| `fix:` | `fix: porta do gateway dinâmica` | **patch** | `X.Y.(Z+1)` |
| `feat:` | `feat: suporte a Azure DevOps` | **minor** | `X.(Y+1).0` |
| `feat!:` / `BREAKING CHANGE:` no rodapé | `feat!: remover config old-style` | **major** | `(X+1).0.0` |
| `docs:`, `chore:`, `refactor:`, `test:`, `style:`, `ci:` | `docs: atualizar README` | **nenhum** | sem release |

A cada release, a pipeline (`.github/workflows/release.yml`, rodando em GitHub
Actions **após o CI passar** no `main` — ela é disparada por `workflow_run` do
workflow `CI` e só prossegue se a conclusão dele foi `success`, de modo que um
`main` vermelho nunca corta uma release):

- Calcula a próxima versão a partir dos commits acumulados.
- Atualiza o **CHANGELOG** automaticamente (agrupado por tipo de commit).
- Estampa a nova versão nas fontes de verdade (ver seção abaixo).
- Cria a **tag** correspondente no GitHub (ex.: `v1.2.0`).

### Ferramenta escolhida: `python-semantic-release`

A ferramenta é o [`python-semantic-release`](https://python-semantic-release.readthedocs.io/),
escolhido porque **este é um projeto Python**:

- A versão autoritativa vive em `pyproject.toml` (`[project].version`) e no
  `app.json` — ambos artefatos Python/JSON.
- **Não existe um `package.json` na raiz** do repositório (o único `package.json`
  fica em `ui/`, para o bundle TypeScript do dashboard, e não é o artefato
  versionado do Crew App).
- Manter o versionamento na toolchain Python evita introduzir uma toolchain de
  release Node (`semantic-release` clássico) só para carimbar versão, mantendo o
  CI coerente com o resto do projeto (`ruff`, `mypy`, `pytest`).

> A execução end-to-end do `python-semantic-release` acontece **no CI (GitHub
> Actions), no push para `main`** — não localmente no sandbox. A validação local
> é estática (parse de YAML/TOML e, quando possível, dry-run), não o corte de uma
> release real.

---

## Fontes de verdade da versão

A versão é carimbada em **dois arquivos** a cada release:

| Fonte de verdade | Campo | Papel |
|---|---|---|
| `pyproject.toml` | `[project].version` | Versão do pacote Python (target do `python-semantic-release`). |
| `app.json` | `"version"` | Versão do Crew App (manifesto que o gateway lê). |

### Drift atual a reconciliar

Existe uma divergência **pré-existente** de versão que precisa ser reconciliada
quando a pipeline for ligada:

- `pyproject.toml` → `[project].version = "0.1.0"`.
- `app.json` → `"version": "1.0.0"`.
- Os endpoints de health retornam a string `"1.0.0"` **hardcoded**:
  - `backend/routes.py` (`handle_health`, ~linha 133).
  - `backend/server.py` (`handle_health`, ~linha 32).

Decisão: as fontes de verdade são `pyproject.toml` e `app.json`; os endpoints de
health **não** devem carregar uma string de versão hardcoded — devem derivar a
versão de uma fonte única (metadados do pacote, com fallback para o `app.json`)
para que o `python-semantic-release` só precise atualizar um lugar canônico e os
endpoints acompanhem automaticamente.

Reconciliação aplicada (FEAT-003):

- **Baseline único: `1.0.0`.** `pyproject.toml` (`[project].version`) foi elevado
  de `0.1.0` para `1.0.0`, alinhando com o valor já publicado no `app.json` e nos
  endpoints de health. Assim o `python-semantic-release` parte de uma baseline
  consistente (a última release efetiva é `v1.0.0`).
- **Health derivado dinamicamente.** `backend/routes.py` e `backend/server.py` não
  carregam mais `"1.0.0"` hardcoded; ambos usam `backend/version.py:get_version()`,
  que lê os metadados do pacote instalado (a versão do `pyproject.toml`) e cai para
  o campo `version` do `app.json` quando o pacote não está instalado.
- **Sincronização do `app.json`.** Como o `python-semantic-release` não carimba uma
  chave JSON diretamente, o `build_command` invoca `scripts/sync_app_version.py`,
  que propaga a nova versão para o `app.json` a cada release.

Os testes já protegem o contrato (`test_app_manifest.py` valida a estrutura do
`app.json` e `test_backend_hooks_routes.py` exige a **presença** da chave
`version`, não um valor específico), então a reconciliação não quebra esse
contrato.

---

## Tags de canal

Dois canais de release, representados por refs/tags no repositório:

| Canal | Significado | Estabilidade |
|---|---|---|
| `latest` | Última versão publicada pela pipeline. | Pode conter instabilidade (recém-saída do `main`). |
| `stable` | Versão **validada para produção**. | Só avança após validação manual. |

- `latest` sempre acompanha a release mais recente: a pipeline aponta/atualiza o
  canal `latest` a cada release cortada no push para `main`.
- `stable` é um ref/tag movível que **só é avançado depois de a versão ter sido
  validada** — nunca automaticamente junto com `latest`. Promover para `stable` é
  uma ação deliberada (manual), coerente com "deploy é sempre manual".
- **O `app.json` aponta para `stable` por padrão.** Uma instalação padrão do Crew
  App consome o canal validado, e nunca a ponta potencialmente instável do
  `main`. Optar por `latest` é uma escolha explícita de quem quer a ponta.

Representação concreta (FEAT-003):

- Cada release cria a tag imutável `vX.Y.Z` (formato `tag_format = "v{version}"`).
- O canal `latest` é uma tag **movível** que o workflow `.github/workflows/release.yml`
  reposiciona (`git tag -f latest <vX.Y.Z> && git push --force origin refs/tags/latest`)
  a cada release cortada. Como a release só roda **após o CI passar** (gating por
  `workflow_run`), o `latest` nunca aponta para um build que falhou lint/type/test.
- O canal `stable` **não** é tocado pelo workflow: avançá-lo é uma ação manual
  deliberada, coerente com "deploy é sempre manual".
- O `app.json` fixa o default via o campo **`"channel": "stable"`**. Uma instalação
  padrão consome o canal validado; optar por `latest` é uma escolha explícita.

---

## Decisão: auto vs manual

Este é o núcleo do item 4. A pergunta é: **quando um update pode se aplicar
sozinho e quando ele precisa esperar uma ação humana?**

A política é derivada diretamente do princípio inviolável — deploy é sempre
manual e a produção nunca pode ser quebrada automaticamente. A consequência é
simples: **só updates comprovadamente compatíveis para trás (patch/`fix`) podem se
aplicar automaticamente.** Qualquer coisa que possa mudar comportamento
(`minor`/`major`) apenas **notifica** e aguarda ação manual.

| Nível (tipo de commit) | Compatibilidade | Comportamento do update | Ação humana? |
|---|---|---|---|
| **Patch** (`fix:`) | Compatível para trás — correções sem mudança de comportamento. | **Auto-update seguro** — aplicado automaticamente. | Não |
| **Minor** (`feat:`) | Nova funcionalidade — pode mudar comportamento. | **Notifica o usuário e aguarda** ação manual. | Sim |
| **Major / breaking** (`feat!:` / `BREAKING CHANGE:`) | Quebra de compatibilidade. | **Notifica o usuário e aguarda** ação manual. | Sim |

### Justificativa (por que patch é a única exceção)

- `fluxo.md` (`inclusion: always`) e o `README` (*Princípios*) afirmam que a
  automação **nunca faz deploy** e que os **gates humanos são invioláveis**.
- Aplicar um `feat`/breaking automaticamente é, na prática, mudar o comportamento
  de produção sem gate humano — exatamente o que o projeto proíbe.
- Um `fix` é, por definição de conventional commits, uma correção compatível para
  trás. Aplicá-lo automaticamente reduz risco (corrige bugs) sem introduzir
  mudança de comportamento, então é a **única** classe de update que pode
  auto-aplicar sem ferir o princípio.
- Resultado: **patch = auto; minor = manual/notifica; major = manual/notifica.**
  A produção nunca é auto-quebrada porque nenhuma mudança de comportamento entra
  sem uma pessoa decidindo.

---

## Mecanismo de update

### O que está sendo substituído

O mecanismo atual é o cron **`flow-auto-update`** (registrado em `app.json`,
`script ~/.kiro/crew/crons/flow_auto_update.py:run`, `every: 300`), implementado em
`scripts/flow_auto_update.py`. A cada 5 minutos ele:

- Descobre o repositório via `installed.json`.
- Executa `git pull --rebase origin main` **incondicionalmente**.
- Reinstala os scripts com `./scripts/install-cron.sh`.

Isso é uma gambiarra: puxa qualquer coisa que estiver no `main` a cada 5 minutos,
**sem controle de versão e sem distinguir patch de feature/breaking**. Ou seja,
pode aplicar automaticamente uma mudança de comportamento em produção — violando o
princípio inviolável.

### Para onde vamos

O update passa a usar o **caminho oficial de update do Crew App**: o hook
`setup.onUpdate` do `app.json`. É assim que os builtins do Kiro Crew (por exemplo,
o *Issue Radar*) se atualizam — pelo hook oficial de update do app, e não por um
cron custom. O hook oficial é disparado pelo gateway no fluxo de update do app, em
vez de um polling de `git pull` a cada 5 minutos.

Esse caminho fica **gated pela política auto vs manual** acima. Um cron leve de
verificação (`flow-update-check`, `every: 3600`, em `scripts/flow_update_check.py`)
descobre a versão do canal `stable` e delega a decisão à lógica pura de domínio
(`flow/domain/update_policy.py`):

- **Patch (`fix`)**: o update **se aplica automaticamente**. Como o
  `install-cron.sh` apenas *copia* o checkout atual (ele nunca faz fetch/checkout),
  o cron primeiro **avança o working tree para a tag imutável `vX.Y.Z`** do canal
  `stable` (`git fetch --tags` + `git checkout vX.Y.Z`) e **só então** reinstala
  pelo caminho oficial (`./scripts/install-cron.sh`, o mesmo do `setup.onUpdate`).
  Ao final, reconfirma que a versão instalada de fato avançou antes de reportar
  sucesso — nunca anuncia um update que não moveu o código.
- **Minor/Major (`feat`/breaking)**: o mecanismo **apenas notifica** o usuário
  (pelo canal de notificação do Crew) e **aguarda ação manual** — nunca aplica
  sozinho. A notificação instrui a atualização manual a **fazer o checkout da tag
  alvo antes** de reinstalar (`git checkout vX.Y.Z && ./scripts/install-cron.sh`,
  ou o hook oficial `setup.onUpdate`), porque só rodar o `install-cron.sh`
  reinstalaria o checkout atual sem avançar a versão.

> **Descoberta do canal robusta.** O `flow-update-check` faz `git fetch --tags`
> antes de comparar; se o fetch **falhar**, ele trata isso como `NOOP` (não decide
> sobre tags locais possivelmente obsoletas). A tag de versão é resolvida casando
> estritamente `vX.Y.Z` e, havendo mais de uma no mesmo commit, escolhendo a maior.

O cron `flow-auto-update` (e o `scripts/flow_auto_update.py`) foi
**removido/substituído** pelo `flow-update-check`, conforme o critério de aceite da
issue (“cron `flow-auto-update` removido ou substituído”).

> **Forward-reference:** a implementação concreta do hook `setup.onUpdate`, o gating
> por nível de versão e a remoção do cron `flow-auto-update` são entregues na
> feature de implementação do mecanismo de update (FEAT-004). Este documento fixa a
> **política e a intenção**; qualquer detalhe de comando fica lá, sem contradizer o
> que está aqui.

---

## Resumo

- **Versionamento:** conventional commits → `python-semantic-release` no CI
  (`feat`→minor, `fix`→patch, breaking→major); CHANGELOG e tag automáticos.
- **Fontes de verdade:** `pyproject.toml` e `app.json`; reconciliar o drift
  `0.1.0` vs `1.0.0` (health endpoints não devem ter versão hardcoded).
- **Canais:** `latest` (ponta, pode instabilizar) e `stable` (validado);
  `app.json` aponta para `stable` por padrão.
- **Auto vs manual:** patch auto-aplica; minor/major notificam e aguardam ação
  manual — porque a produção nunca pode ser auto-quebrada.
- **Update:** cron `flow-auto-update` (gambiarra) → hook oficial `setup.onUpdate`
  do Crew App, gated pela política acima; cron removido após validação.
