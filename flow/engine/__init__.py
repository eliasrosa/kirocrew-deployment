"""Motor de orquestração do KiroCrew Flow.

``ledger_tick`` — motor ledger-driven do single-flow: uma cron única que lê o
estado da task ativa no ``RunLedger`` (SQLite local) e a empurra estágio a
estágio, com custo de rede O(1) por tick. Orquestração pura; o disparo real
(worktree + sessão one-shot) fica atrás da porta ``Dispatcher``.
"""
