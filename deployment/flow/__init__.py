"""KiroCrew Flow — módulos de cron por estágio.

Cada módulo expõe uma função ``run(ctx)`` que o Kiro Crew invoca como script de cron.

Módulos disponíveis:
    dev           — flow:develop-waiting → dispatch dev
    reviewer      — flow:review-waiting  → dispatch reviewer
    merge         — flow:review-approved / flow:qa-approved → merge squash
    conflict      — flow:merge-conflict  → dispatch resolução de conflito
    rework        — flow:review-refused  → gate humano, notifica TL
    qa_notify     — flow:qa-waiting      → notifica QA
    qa_refused    — flow:qa-refused      → gate humano, notifica TL+dev

A lógica comum (config, locks, worktree, dispatch genérico, scan) está em ``base``.
"""
