"""Executor do KiroCrew Flow — Fase 1.

Recebe um ScanResult do scan e decide o que fazer:
  - determina o template (feature/bug/hotfix/debt)
  - aplica o GATE 0 (triagem, pode trocar de template)
  - valida pré-condições (COV para debt, hml-bypass para hotfix)
  - decide a ação (dispatch, notify_human, block)
  - retorna uma ExecutorDecision sem executar I/O

O I/O (atualizar labels, postar comentário, disparar agente) fica na
camada de calling (deployment.py ou o futuro executor assíncrono).
"""
