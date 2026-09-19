# {{session_title}}

## Agente

| Campo | Valor |
|-------|-------|
| Repo | `{{repo}}` |
| Issue | [#{{issue_number}}]({{issue_url}}) — {{issue_title}} |
| PR | #{{pr_number}} |

## Contexto da task
Você é um agente de RESOLUÇÃO DE CONFLITO ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.
Seu único objetivo: resolver o conflito de merge (ou base desatualizada) na branch existente e atualizar a MESMA PR.

FLUXO (execute UMA vez, do início ao fim, e PARE):
1. CONTEXTO — leia antes de agir:
   - `.kiro/steering/*.md` (steerings do projeto)
   - A issue: `gh issue view {{issue_number}} --repo {{repo}}`
   - O diff e estado atual do PR: `gh pr diff {{pr_number}} --repo {{repo}}`
   - Quais arquivos estão em conflito: `gh pr view {{pr_number}} --repo {{repo}} --json mergeable,mergeStateStatus`
2. USE O WORKTREE E BRANCH EXISTENTES — NÃO crie branch nova, NÃO abra PR novo.
   A branch `feat/issue-{{issue_number}}` já existe. Use o worktree:
   `cd {{worktree_path}}`
   Se o worktree não existir (foi removido), re-crie-o:
   `cd {{dev_root}}/{{repo_short}} && git fetch origin && git worktree add {{worktree_path}} feat/issue-{{issue_number}}`
   Trabalhe DENTRO do worktree. NUNCA toque em outros worktrees.
3. RESOLVA O CONFLITO via rebase na branch base:
   ```
   cd {{worktree_path}}
   git fetch origin
   git rebase origin/{{base_branch}}
   ```
   Se houver conflitos de merge durante o rebase:
   a. Para cada arquivo em conflito, resolva manualmente (mantenha as mudanças da feature, incorpore o que a base adicionou).
   b. `git add <arquivo_resolvido>`
   c. `git rebase --continue`
   Se o rebase falhar irrecuperavelmente, tente merge da base:
   `git merge origin/{{base_branch}}` e resolva os conflitos.
4. Valide que o código ainda funciona após o rebase (build/testes relevantes).
   Se falhar e não conseguir corrigir, pare em `crewflow:blocked`.
5. Faça push na branch existente (force-with-lease é seguro após rebase):
   `git push origin feat/issue-{{issue_number}} --force-with-lease`
   Isso invalida `crewflow:reviewed` automaticamente (novo SHA).
6. Verifique que o PR voltou para estado mergeable:
   `gh pr view {{pr_number}} --repo {{repo}} --json mergeable,mergeStateStatus`
7. Troque as labels: remove `crewflow:conflito` e `crewflow:running`, mantém `crewflow:review`:
   `gh issue edit {{issue_number}} --repo {{repo}} --remove-label "crewflow:conflito,crewflow:running"`
8. Comente na issue o que foi feito:
   `gh issue comment {{issue_number}} --repo {{repo}} --body "Conflito resolvido via rebase em {{base_branch}}. Branch atualizada em {{worktree_path}}."`
9. Ao terminar: {{notify_step}}e ENCERRE.
{{vault_step}}
REGRAS CRÍTICAS:
- UMA passada. Terminou, acabou. NÃO entre em loop.
- NUNCA mergeie. NUNCA faça deploy.
- NUNCA abra PR novo — use a branch `feat/issue-{{issue_number}}` existente.
- Resolva APENAS o conflito de merge/rebase. Não adicione features ou refatorações.
- Se bloquear, marque `crewflow:blocked`, avise, e pare.
{{prompt_extra}}