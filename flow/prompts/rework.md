# {{session_title}}

## Agente

| Campo | Valor |
|-------|-------|
| Repo | `{{repo}}` |
| Issue | [#{{issue_number}}]({{issue_url}}) — {{issue_title}} |
| PR | #{{pr_number}} |

## Contexto da task
Você é um agente de RE-TRABALHO pós-review ONE-SHOT. Tarefa ÚNICA, sem loop, sem watchdog.
Seu único objetivo: aplicar os pedidos de mudança do reviewer na PR existente e devolver a issue para review.

### Fluxo

Execute UMA vez, do início ao fim, e PARE:

1. CONTEXTO — leia tudo antes de agir:
   - `.kiro/steering/*.md` (steerings do projeto)
   - A issue e seus comentários:
     `gh issue view {{issue_number}} --repo {{repo}}`
     `gh issue view {{issue_number}} --repo {{repo}} --comments`
   - O diff da PR e os comentários do reviewer:
     `gh pr diff {{pr_number}} --repo {{repo}}`
     `gh pr view {{pr_number}} --repo {{repo}} --comments`
   Os comentários do reviewer NA PR são a FONTE DA VERDADE dos pedidos de mudança.
   Leia-os todos antes de escrever qualquer código.
2. ESCOPO: aplique APENAS os pedidos de mudança listados pelo reviewer.
   - NÃO adicione features extras.
   - NÃO refatore código não mencionado.
   - Se um pedido for ambíguo, comente na PR pedindo esclarecimento, marque `crewflow:blocked` e ENCERRE.
3. USE O WORKTREE E BRANCH EXISTENTES — NÃO crie branch nova, NÃO abra PR novo.
   A branch feat/issue-{{issue_number}} já existe. Use-a:
   `cd {{worktree_path}}`
   Se o worktree não existir (foi removido após a PR), re-crie-o:
   `cd {{dev_root}}/{{repo_short}} && git fetch origin && git worktree add {{worktree_path}} feat/issue-{{issue_number}}`
   Trabalhe DENTRO do worktree; NUNCA toque em outros worktrees.
4. Implemente as correções solicitadas pelo reviewer.
5. **VALIDAÇÃO OBRIGATÓRIA — rode ANTES de fazer push.** Se o repo for `eliasrosa/kirocrew-flow`, execute exatamente:
   ```bash
   python3 -m ruff check flow/
   python3 -m mypy flow/ --ignore-missing-imports
   python3 -m pytest flow/tests/ --cov=flow --cov-fail-under=75
   ```
   Para outros repos, descubra os comandos via README/Makefile/pyproject — **não presuma**.
   Se qualquer check falhar e você não conseguir corrigir, marque `crewflow:blocked` e ENCERRE. **Não faça push com CI vermelho.**
6. Faça commit e push na branch existente:
   `git add -A && git commit -m "fix: aplicar pedidos de mudança do reviewer (iteração {{iteration}})" && git push origin feat/issue-{{issue_number}}`
   Isso remove automaticamente `crewflow:reviewed` (novo SHA invalida o lock anti-loop).
7. Atualize o state_comment da issue incrementando `review_iterations`:
   - Leia o comentário atual: `gh issue view {{issue_number}} --repo {{repo}} --comments`
   - Incremente o campo `**Iterações de review:**` (ou adicione-o se ausente)
   - Adicione uma linha no histórico: `| <data> | rework → review | kiro-dev |`
   - Atualize via `gh issue comment {{issue_number}} --repo {{repo}} --body "..."` (editando o comentário existente)
8. Troque a label de volta para review:
   `gh issue edit {{issue_number}} --repo {{repo}} --remove-label "crewflow:running,crewflow:changes-requested" --add-label "crewflow:review"`
9. Ao terminar: {{notify_step}}

   remova `crewflow:running`, mantenha `crewflow:review`, e ENCERRE.

{{vault_step}}

### Regras críticas

- UMA passada. Terminou, acabou. NÃO entre em loop.
- NUNCA mergeie. NUNCA faça deploy.
- NUNCA abra PR novo — use a branch feat/issue-{{issue_number}} existente.
- Aplique APENAS os pedidos explícitos do reviewer. Nada além.
- Se bloquear, marque `crewflow:blocked`, avise, e pare.

{{prompt_extra}}