"""Factory de transport SCM — seleciona GitHub ou Azure DevOps por repo.

Uso típico
----------
::

    from flow.adapters.scm.factory import ScmTransportFactory, ScmRepoConfig

    config = ScmRepoConfig(
        scm="azure_devops",
        azure_org="https://dev.azure.com/kdop",
        azure_project="PlataformaCogna-MKTP-MVP",
        azure_repo="voomp-creators-api-gateway2",
    )
    factory = ScmTransportFactory(config)
    factory.create_pull_request(branch="feat/123", title="...", body="...")

Esquema no squad YAML
---------------------
::

    repos:
      - name: kdop/api-gateway2
        scm: azure_devops
        azure_org: https://dev.azure.com/kdop
        azure_project: PlataformaCogna-MKTP-MVP
        azure_repo: voomp-creators-api-gateway2

    # Para GitHub (default se scm omitido):
      - name: org/frontend
        scm: github

Métodos disponíveis
-------------------
A factory expõe a mesma superfície para ambos os providers:

- ``create_pull_request(branch, title, body, reviewers, target_branch)``
- ``list_pull_requests(state, source_branch)``
- ``get_pull_request(pr_number)``
- ``merge_pull_request(pr_number, merge_method)``
- ``delete_branch(branch)``
- ``list_reviews(pr_number)``
- ``post_comment(pr_number, body)``
- ``get_pr_for_issue(issue_number)`` ← somente GitHub
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Configuração de repo por SCM
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ScmRepoConfig:
    """Configuração de SCM para um repositório.

    Campos obrigatórios dependem do ``scm``:
      - ``github``       : ``owner_repo`` (ex: ``eliasrosa/kirocrew-flow``)
      - ``azure_devops`` : ``azure_org``, ``azure_project``, ``azure_repo``
    """

    scm: str = "github"
    # GitHub
    owner_repo: str = ""
    # Azure DevOps
    azure_org: str = ""
    azure_project: str = ""
    azure_repo: str = ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class ScmTransportFactory:
    """Fachada unificada de SCM — delega ao transport correto.

    Instancie com um ``ScmRepoConfig``; todos os métodos são válidos para
    qualquer SCM configurado.

    Lança ``ValueError`` se o ``scm`` for desconhecido.
    """

    _SUPPORTED: frozenset[str] = frozenset({"github", "azure_devops"})

    def __init__(self, config: ScmRepoConfig) -> None:
        if config.scm not in self._SUPPORTED:
            raise ValueError(
                f"SCM não suportado: {config.scm!r}. "
                f"Valores aceitos: {sorted(self._SUPPORTED)}"
            )
        self._config = config

    # ------------------------------------------------------------------
    # Pull Requests
    # ------------------------------------------------------------------

    def create_pull_request(
        self,
        branch: str,
        title: str,
        body: str,
        reviewers: list[str] | None = None,
        target_branch: str = "main",
    ) -> dict:
        """Abre um PR no SCM configurado."""
        if self._config.scm == "azure_devops":
            from flow.adapters.scm import azure_devops as ado
            return ado.create_pull_request(
                org=self._config.azure_org,
                project=self._config.azure_project,
                repo=self._config.azure_repo,
                branch=branch,
                title=title,
                body=body,
                reviewers=reviewers,
                target_branch=target_branch,
            )
        # GitHub
        from flow.adapters import github_transport as gh
        result = gh._run([  # type: ignore[attr-defined]
            "pr", "create",
            "--repo", self._config.owner_repo,
            "--head", branch,
            "--base", target_branch,
            "--title", title,
            "--body", body,
        ])
        return result if isinstance(result, dict) else {"url": result}

    def list_pull_requests(
        self,
        state: str = "active",
        source_branch: str | None = None,
    ) -> list:
        """Lista PRs. ``state`` = ``active``/``completed`` (ADO) ou ``open``/``closed`` (GH)."""
        if self._config.scm == "azure_devops":
            from flow.adapters.scm import azure_devops as ado
            return ado.list_pull_requests(
                org=self._config.azure_org,
                project=self._config.azure_project,
                repo=self._config.azure_repo,
                state=state,
                source_branch=source_branch,
            )
        # GitHub
        from typing import cast

        from flow.adapters import github_transport as gh
        gh_state = "open" if state == "active" else state
        args = [
            "pr", "list",
            "--repo", self._config.owner_repo,
            "--state", gh_state,
            "--json", "number,title,headRefName,headRefOid,body,mergeable",
            "--limit", "50",
        ]
        if source_branch:
            args += ["--head", source_branch]
        return cast(list, gh._run(args))

    def get_pull_request(self, pr_number: int) -> dict:
        """Retorna os detalhes de um PR."""
        if self._config.scm == "azure_devops":
            from flow.adapters.scm import azure_devops as ado
            return ado.get_pull_request(
                org=self._config.azure_org,
                project=self._config.azure_project,
                repo=self._config.azure_repo,
                pr_number=pr_number,
            )
        # GitHub
        from typing import cast

        from flow.adapters import github_transport as gh
        return cast(dict, gh._run([
            "pr", "view", str(pr_number),
            "--repo", self._config.owner_repo,
            "--json", "number,title,headRefName,headRefOid,body,mergeable,state",
        ]))

    def merge_pull_request(
        self,
        pr_number: int,
        merge_method: str = "squash",
    ) -> dict:
        """Faz o merge de um PR."""
        if self._config.scm == "azure_devops":
            from flow.adapters.scm import azure_devops as ado
            # Mapeia nomes de estratégia GitHub→ADO
            strategy_map = {
                "squash": "squash",
                "merge": "noFastForward",
                "rebase": "rebase",
            }
            ado_strategy = strategy_map.get(merge_method, "squash")
            return ado.merge_pull_request(
                org=self._config.azure_org,
                project=self._config.azure_project,
                repo=self._config.azure_repo,
                pr_number=pr_number,
                merge_strategy=ado_strategy,
            )
        # GitHub
        from flow.adapters import github_transport as gh
        return gh.merge_pull_request(
            self._config.owner_repo,
            pr_number,
            merge_method=merge_method,
        )

    def delete_branch(self, branch: str) -> None:
        """Deleta um branch remoto. Silencioso se não existir."""
        if self._config.scm == "azure_devops":
            from flow.adapters.scm import azure_devops as ado
            ado.delete_branch(
                org=self._config.azure_org,
                project=self._config.azure_project,
                repo=self._config.azure_repo,
                branch=branch,
            )
            return
        from flow.adapters import github_transport as gh
        gh.delete_branch(self._config.owner_repo, branch)

    def list_reviews(self, pr_number: int) -> list:
        """Retorna os reviews/votos de um PR.

        Retorna lista normalizada com ``state`` = APPROVED | CHANGES_REQUESTED | PENDING.
        """
        if self._config.scm == "azure_devops":
            from flow.adapters.scm import azure_devops as ado
            return ado.list_reviews(
                org=self._config.azure_org,
                project=self._config.azure_project,
                repo=self._config.azure_repo,
                pr_number=pr_number,
            )
        from flow.adapters import github_transport as gh
        return gh.get_pr_reviews(self._config.owner_repo, pr_number)

    def post_comment(self, pr_number: int, body: str) -> dict:
        """Posta um comentário no PR."""
        if self._config.scm == "azure_devops":
            from flow.adapters.scm import azure_devops as ado
            return ado.post_comment(
                org=self._config.azure_org,
                project=self._config.azure_project,
                repo=self._config.azure_repo,
                pr_number=pr_number,
                body=body,
            )
        from flow.adapters import github_transport as gh
        return gh.create_pr_comment(self._config.owner_repo, pr_number, body)

    # ------------------------------------------------------------------
    # GitHub-specific (não disponível no ADO via esta factory)
    # ------------------------------------------------------------------

    def get_pr_for_issue(self, issue_number: int) -> dict | None:
        """Retorna o PR aberto associado à issue (GitHub only).

        Lança ``NotImplementedError`` para Azure DevOps — use
        ``list_pull_requests(source_branch='feat/issue-N')`` nesse caso.
        """
        if self._config.scm == "azure_devops":
            raise NotImplementedError(
                "get_pr_for_issue não está disponível para Azure DevOps. "
                "Use list_pull_requests(source_branch='feat/issue-N') para localizar o PR."
            )
        from flow.adapters import github_transport as gh
        return gh.get_pr_for_issue(self._config.owner_repo, issue_number)


# ---------------------------------------------------------------------------
# Helpers de construção a partir do squad YAML
# ---------------------------------------------------------------------------

def scm_config_from_repo_entry(entry: dict) -> ScmRepoConfig:
    """Constrói um ``ScmRepoConfig`` a partir de um entry do campo ``repos`` do YAML.

    Formatos suportados::

        # Azure DevOps — campos explícitos (recomendado)
        repos:
          - name: kdop/api-gateway2
            scm: azure_devops
            azure_org: https://dev.azure.com/kdop
            azure_project: PlataformaCogna-MKTP-MVP
            azure_repo: voomp-creators-api-gateway2

        # Azure DevOps — detecção automática pelo prefixo de URL
        # ``scm`` é inferido quando ``name`` começa com ``dev.azure.com``
          - name: dev.azure.com/kdop/PlataformaCogna-MKTP-MVP/voomp-creators-api-gateway2
          # equivale a scm=azure_devops com os campos derivados da URL

          - name: org/frontend      # scm omitido → github

    A detecção automática ocorre quando ``scm`` está ausente E o campo
    ``name`` segue o padrão ``dev.azure.com/<org>/<project>/<repo>`` (com ou
    sem o prefixo ``https://``).  Se os campos ``azure_org``, ``azure_project``
    ou ``azure_repo`` forem fornecidos explicitamente, sobrepõem os derivados.
    """
    scm = str(entry.get("scm", "")).strip()
    name = str(entry.get("name", "")).strip()

    # Detecta Azure DevOps automaticamente pela URL
    if not scm:
        scm = _infer_scm_from_name(name)

    if scm == "azure_devops":
        # Tenta derivar org/project/repo da URL quando não fornecidos explicitamente
        derived = _parse_azure_devops_url(name)
        return ScmRepoConfig(
            scm="azure_devops",
            azure_org=str(entry.get("azure_org", "") or derived.get("org", "")).strip(),
            azure_project=str(entry.get("azure_project", "") or derived.get("project", "")).strip(),
            azure_repo=str(entry.get("azure_repo", "") or derived.get("repo", "")).strip(),
        )

    # GitHub (default)
    return ScmRepoConfig(scm="github", owner_repo=name)


# ---------------------------------------------------------------------------
# Helpers de detecção automática de SCM
# ---------------------------------------------------------------------------

def _infer_scm_from_name(name: str) -> str:
    """Infere o SCM a partir do campo ``name`` de um entry de repo.

    Retorna ``"azure_devops"`` se o nome seguir o padrão de URL do Azure DevOps
    (``dev.azure.com/...``), caso contrário retorna ``"github"``.
    """
    # Match ancorado no início: exige que ``dev.azure.com/`` seja o host logo
    # após um schema opcional. Isso elimina a ambiguidade de posição que o
    # engine de dataflow do CodeQL rastreava com ``removeprefix()`` +
    # ``startswith()`` (o host podia, em tese, aparecer em posição arbitrária).
    if re.match(r"^(?:https?://)?dev\.azure\.com/", name.lower()):
        return "azure_devops"
    return "github"


def _parse_azure_devops_url(name: str) -> dict:
    """Deriva ``org``, ``project`` e ``repo`` de uma URL do Azure DevOps.

    Formatos aceitos::

        dev.azure.com/<org>/<project>/<repo>
        https://dev.azure.com/<org>/<project>/<repo>

    Retorna dict com as chaves ``org``, ``project``, ``repo`` (strings vazias
    se o padrão não casar — o chamador usa campos explícitos do YAML como
    fallback).
    """
    # Remove schema
    stripped = name
    for prefix in ("https://", "http://"):
        if stripped.lower().startswith(prefix):
            stripped = stripped[len(prefix):]
            break

    # Remove prefixo dev.azure.com/
    prefix = "dev.azure.com/"
    if stripped.lower().startswith(prefix):
        stripped = stripped[len(prefix):]
    else:
        return {}

    parts = stripped.split("/")
    if len(parts) < 3:
        return {}

    return {
        "org": f"https://dev.azure.com/{parts[0]}",
        "project": parts[1],
        "repo": parts[2],
    }
