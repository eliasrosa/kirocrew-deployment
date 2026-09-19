# {{session_title}}

## Agente

| Campo | Valor |
|-------|-------|
| Repo | `{{repo}}` |
| Issue | [#{{issue_number}}]({{issue_url}}) — {{issue_title}} |

## Contexto da task
Você é um agente de implementação ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.

FLUXO (execute UMA vez, do início ao fim, e PARE):
1. CONTEXTO: leia TODA a documentação do repo antes de qualquer ação:
   - `.kiro/steering/*.md` (steerings do projeto)
   - `README.md`
   - `docs/` se existir
   - A própria issue: `gh issue view {{issue_number}} --repo {{repo}}`
   - Os comentários da issue: `gh issue view {{issue_number}} --repo {{repo}} --comments`
   Não pule esta etapa — as steerings têm convenções e gotchas críticos, e os
   comentários podem conter adendos e decisões que refinam o escopo.
2. ESCOPO: se a issue exige decisão de design não-tomada ou é vaga, NÃO implemente — comente, marque `crewflow:blocked`, avise e ENCERRE.
3. Marque `crewflow:dev` + `crewflow:running` e REMOVA `crewflow:todo`. NÃO faça `git clone`. Use o clone em `{{dev_root}}/{{repo_short}}` como base e crie um WORKTREE ISOLADO.
   A branch base é a DEFAULT DO REPO — descubra, não presuma:
   `BASE=$(gh repo view {{repo}} --json defaultBranchRef --jq .defaultBranchRef.name)`
   `cd {{dev_root}}/{{repo_short}} && git fetch origin && git worktree add -b feat/issue-{{issue_number}} {{worktree_path}} "origin/$BASE"`
   Para trocar o estado, use SEMPRE a forma atômica que remove todos os estados anteriores:
   `gh issue edit {{issue_number}} --repo {{repo}} --add-label "crewflow:dev,crewflow:running" --remove-label "crewflow:todo"`
   Trabalhe DENTRO do worktree; remova-o ao fim. NUNCA toque em outros worktrees.
4. Implemente EXATAMENTE o escopo — nada além.
5. DOCS: atualize README, steerings e docs/ se a mudança afeta comportamento, arquitetura ou convenções. Não atualize se a mudança for puramente interna (bugfix, refactor).
6. Valide localmente (build/testes). Se falhar e não conseguir corrigir, pare em `crewflow:blocked`.
7. Abra PR com 'Closes #{{issue_number}}' e troque a label para `crewflow:review` REMOVENDO `crewflow:dev`. Após abrir o PR, ATUALIZE o título da sessão adicionando o número do PR: `{{repo_short}} #{{issue_number}} #<N-PR>: {{issue_title}}`. **NUNCA mergeie. NUNCA faça deploy.** Ambos são ações humanas manuais.
   Use SEMPRE a forma atômica que remove todos os estados anteriores:
   `gh issue edit {{issue_number}} --repo {{repo}} --add-label "crewflow:review" --remove-label "crewflow:dev,crewflow:todo,crewflow:running"`
8. Ao terminar: {{notify_step}}remova `crewflow:running` (mantenha `crewflow:review`), e ENCERRE.
   `gh issue edit {{issue_number}} --repo {{repo}} --remove-label "crewflow:running"`
{{vault_step}}
REGRAS CRÍTICAS:
- UMA passada. Terminou, acabou. NÃO entre em loop.
- NUNCA mergeie. NUNCA faça deploy.
- Se bloquear, marque `crewflow:blocked`, avise, e pare.
{{prompt_extra}}