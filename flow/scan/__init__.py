"""Scanner zero-token do KiroCrew Flow.

O coração do sistema: varre issues dos providers (Jira/GitHub) sem gastar
token de agente. Só processa quando detecta mudança de labels via hash.

  scan_candidates(squad_config, provider) -> list[ScanResult]

Retorna apenas as issues candidatas a dispatch neste ciclo.
O agente só é acordado quando há trabalho real.
"""
