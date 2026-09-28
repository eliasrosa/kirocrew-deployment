"""Adapters de SCM (Source Control Management).

Contém transportes para sistemas de controle de versão:
  - GithubTransport  — wrapper sobre o ``gh`` CLI
  - AzureDevOpsTransport — REST API do Azure DevOps

Use ``ScmTransportFactory`` para obter o transport correto pelo ``scm``
configurado em um repo no squad YAML.
"""
