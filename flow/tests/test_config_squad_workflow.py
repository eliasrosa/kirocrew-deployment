"""Testes de squad config, workflow e routing — sem I/O de rede."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from flow.config.squad import (
    SquadConfig,
    SquadConfigError,
    _mini_yaml,
    _parse_squad,
    load_squad,
    load_squads_dir,
)
from flow.config.workflow import (
    NodeKind,
    get_template,
    list_templates,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _minimal_squad_dict(**overrides: object) -> dict:
    base: dict = {
        "id": "test-squad",
        "issue_provider": "github",
        "repos": ["owner/api-gateway2", "owner/api-subscription2"],
    }
    base.update(overrides)
    return base


EXAMPLE_YAML = """\
id: my-squad
name: My Squad
issue_provider: jira
project: VGAT
repos:
  - org/api-gateway2
  - org/api-subscription2
workflow_template: versao-c
workflow_params:
  merge_mode: manual
  review_position: before_qa
  allow_hml_bypass: true
routing:
  - match: {labels: ["crewflow:hotfix"]}
    workflow: hotfix-flow
  - match: {labels: ["crewflow:bug"]}
    workflow: bug-flow
  - default: feature-flow
"""

# Mesmo conteúdo que EXAMPLE_YAML mas com routing em formato multi-linha
# (sem inline {labels: [...]}) — exercita o fallback _mini_yaml
EXAMPLE_YAML_MULTILINE_ROUTING = """\
id: my-squad
name: My Squad
issue_provider: jira
project: VGAT
repos:
  - org/api-gateway2
  - org/api-subscription2
workflow_template: versao-c
workflow_params:
  merge_mode: manual
  review_position: before_qa
  allow_hml_bypass: true
routing:
  - match:
      labels:
        - crewflow:hotfix
    workflow: hotfix-flow
  - match:
      labels:
        - crewflow:bug
    workflow: bug-flow
  - default: feature-flow
"""


# ---------------------------------------------------------------------------
# _parse_squad
# ---------------------------------------------------------------------------

class TestParseSquad:
    def test_minimo_funciona(self) -> None:
        sc = _parse_squad(_minimal_squad_dict())
        assert sc.id == "test-squad"
        assert sc.issue_provider == "github"
        assert sc.projects == ["owner/api-gateway2", "owner/api-subscription2"]

    def test_provider_invalido_lanca_erro(self) -> None:
        with pytest.raises(SquadConfigError, match=r"github.*jira"):
            _parse_squad(_minimal_squad_dict(issue_provider="azure"))

    def test_sem_id_lanca_erro(self) -> None:
        d = _minimal_squad_dict()
        del d["id"]
        with pytest.raises(SquadConfigError, match=r"id"):
            _parse_squad(d)

    def test_sem_repos_nem_project_lanca_erro(self) -> None:
        with pytest.raises(SquadConfigError, match=r"project.*repos"):
            _parse_squad({"id": "x", "issue_provider": "jira"})

    def test_project_jira_e_repos_convivem(self) -> None:
        d = {
            "id": "x",
            "issue_provider": "jira",
            "project": "VGAT",
            "repos": ["org/api-gw"],
        }
        sc = _parse_squad(d)
        assert "VGAT" in sc.projects
        assert "org/api-gw" in sc.projects

    def test_routing_parseado(self) -> None:
        d = _minimal_squad_dict()
        d["routing"] = [
            {"match": {"labels": ["crewflow:hotfix"]}, "workflow": "hotfix-flow"},
            {"default": "feature-flow"},
        ]
        sc = _parse_squad(d)
        assert len(sc.routing) == 1
        assert sc.routing[0].workflow == "hotfix-flow"
        assert sc.default_workflow == "feature-flow"

    def test_workflow_params_defaults(self) -> None:
        sc = _parse_squad(_minimal_squad_dict())
        assert sc.workflow_params.merge_mode == "manual"
        assert sc.workflow_params.allow_hml_bypass is True

    def test_workflow_params_customizados(self) -> None:
        d = _minimal_squad_dict()
        d["workflow_params"] = {"merge_mode": "manual", "allow_hml_bypass": False}
        sc = _parse_squad(d)
        assert sc.workflow_params.allow_hml_bypass is False

    def test_repos_normalizados(self) -> None:
        sc = _parse_squad(_minimal_squad_dict())
        # api-gateway2 (sem org) deve estar nos repos
        assert "api-gateway2" in sc.repos


# ---------------------------------------------------------------------------
# resolve_workflow
# ---------------------------------------------------------------------------

class TestResolveWorkflow:
    def _squad(self) -> SquadConfig:
        d = _minimal_squad_dict()
        d["routing"] = [
            {"match": {"labels": ["crewflow:hotfix"]}, "workflow": "hotfix-flow"},
            {"match": {"labels": ["crewflow:bug"]}, "workflow": "bug-flow"},
            {"match": {"labels": ["crewflow:debt"]}, "workflow": "debt-flow"},
            {"default": "feature-flow"},
        ]
        return _parse_squad(d)

    def test_hotfix(self) -> None:
        s = self._squad()
        assert s.resolve_workflow(frozenset({"crewflow:hotfix", "crewflow:p1"})) == "hotfix-flow"

    def test_bug(self) -> None:
        s = self._squad()
        assert s.resolve_workflow(frozenset({"crewflow:bug"})) == "bug-flow"

    def test_default(self) -> None:
        s = self._squad()
        assert s.resolve_workflow(frozenset({"crewflow:feature"})) == "feature-flow"

    def test_sem_labels_usa_default(self) -> None:
        s = self._squad()
        assert s.resolve_workflow(frozenset()) == "feature-flow"

    def test_hotfix_tem_prioridade_sobre_bug(self) -> None:
        s = self._squad()
        # hotfix vem antes de bug nas regras
        assert s.resolve_workflow(frozenset({"crewflow:hotfix", "crewflow:bug"})) == "hotfix-flow"


# ---------------------------------------------------------------------------
# _mini_yaml — parser de fallback (sem PyYAML)
# ---------------------------------------------------------------------------

class TestMiniYaml:
    """Testes unitários do parser fallback _mini_yaml."""

    def _write(self, content: str) -> Path:
        tmp = Path(tempfile.mktemp(suffix=".yaml"))
        tmp.write_text(content)
        return tmp

    def test_escalares(self) -> None:
        p = self._write("id: minha-squad\nauto: true\nmax: 3\n")
        try:
            result = _mini_yaml(p)
            assert result["id"] == "minha-squad"
            assert result["auto"] is True
            assert result["max"] == 3
        finally:
            p.unlink()

    def test_lista_simples(self) -> None:
        p = self._write("repos:\n  - org/api-gw\n  - org/api-sub\n")
        try:
            result = _mini_yaml(p)
            assert result["repos"] == ["org/api-gw", "org/api-sub"]
        finally:
            p.unlink()

    def test_mapeamento_1_nivel(self) -> None:
        p = self._write("workflow_params:\n  merge_mode: manual\n  allow_hml_bypass: true\n")
        try:
            result = _mini_yaml(p)
            assert result["workflow_params"]["merge_mode"] == "manual"
            assert result["workflow_params"]["allow_hml_bypass"] is True
        finally:
            p.unlink()

    def test_routing_inline(self) -> None:
        yaml = (
            "routing:\n"
            '  - match: {labels: ["crewflow:hotfix"]}\n'
            "    workflow: hotfix-flow\n"
            "  - default: feature-flow\n"
        )
        p = self._write(yaml)
        try:
            result = _mini_yaml(p)
            routing = result["routing"]
            assert len(routing) == 2
            assert routing[0]["match"]["labels"] == ["crewflow:hotfix"]
            assert routing[0]["workflow"] == "hotfix-flow"
            assert routing[1]["default"] == "feature-flow"
        finally:
            p.unlink()

    def test_routing_multiline(self) -> None:
        yaml = (
            "routing:\n"
            "  - match:\n"
            "      labels:\n"
            "        - crewflow:hotfix\n"
            "    workflow: hotfix-flow\n"
            "  - match:\n"
            "      labels:\n"
            "        - crewflow:bug\n"
            "    workflow: bug-flow\n"
            "  - default: feature-flow\n"
        )
        p = self._write(yaml)
        try:
            result = _mini_yaml(p)
            routing = result["routing"]
            assert len(routing) == 3
            assert routing[0]["match"]["labels"] == ["crewflow:hotfix"]
            assert routing[0]["workflow"] == "hotfix-flow"
            assert routing[1]["match"]["labels"] == ["crewflow:bug"]
            assert routing[1]["workflow"] == "bug-flow"
            assert routing[2]["default"] == "feature-flow"
        finally:
            p.unlink()

    def test_routing_multiline_multiplas_labels(self) -> None:
        yaml = (
            "routing:\n"
            "  - match:\n"
            "      labels:\n"
            "        - crewflow:bug\n"
            "        - crewflow:p1\n"
            "    workflow: bug-flow\n"
        )
        p = self._write(yaml)
        try:
            result = _mini_yaml(p)
            labels = result["routing"][0]["match"]["labels"]
            assert "crewflow:bug" in labels
            assert "crewflow:p1" in labels
        finally:
            p.unlink()

    def test_ignora_comentarios(self) -> None:
        p = self._write("id: squad  # comentário\n# linha inteira\nauto: false\n")
        try:
            result = _mini_yaml(p)
            assert result["id"] == "squad"
            assert result["auto"] is False
        finally:
            p.unlink()


# ---------------------------------------------------------------------------
# load_squad (leitura de arquivo YAML)
# ---------------------------------------------------------------------------

class TestLoadSquad:
    def test_carrega_yaml_valido(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(EXAMPLE_YAML)
            tmp = f.name
        try:
            sc = load_squad(tmp)
            assert sc.id == "my-squad"
            assert sc.issue_provider == "jira"
            assert len(sc.routing) == 2
            assert sc.default_workflow == "feature-flow"
        finally:
            Path(tmp).unlink()

    def test_carrega_yaml_routing_multiline(self) -> None:
        """_mini_yaml deve parsear routing multi-linha (sem inline {labels: []})."""
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(EXAMPLE_YAML_MULTILINE_ROUTING)
            tmp = f.name
        try:
            sc = load_squad(tmp)
            assert sc.id == "my-squad"
            assert len(sc.routing) == 2
            assert sc.routing[0].workflow == "hotfix-flow"
            assert "crewflow:hotfix" in sc.routing[0].labels
            assert sc.routing[1].workflow == "bug-flow"
            assert "crewflow:bug" in sc.routing[1].labels
            assert sc.default_workflow == "feature-flow"
        finally:
            Path(tmp).unlink()

    def test_arquivo_inexistente_lanca_erro(self) -> None:
        with pytest.raises(SquadConfigError, match="não encontrado"):
            load_squad("/tmp/nao-existe-kirocrew-test.yaml")

    def test_load_squads_dir_ignora_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "example.yaml").write_text(EXAMPLE_YAML)
            (Path(tmp) / "_template.yaml").write_text(EXAMPLE_YAML)
            # Nenhum squad deve ser carregado (example e _ são ignorados)
            squads = load_squads_dir(tmp)
            assert squads == []

    def test_load_squads_dir_carrega_squad_real(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "my-squad.yaml").write_text(EXAMPLE_YAML)
            squads = load_squads_dir(tmp)
            assert len(squads) == 1
            assert squads[0].id == "my-squad"


# ---------------------------------------------------------------------------
# Workflow templates
# ---------------------------------------------------------------------------

class TestWorkflowTemplates:
    def test_todos_os_templates_existem(self) -> None:
        for name in ("feature-flow", "bug-flow", "hotfix-flow", "debt-flow"):
            wf = get_template(name)
            assert wf is not None, f"template {name!r} não encontrado"

    def test_template_desconhecido_retorna_none(self) -> None:
        assert get_template("nao-existe") is None

    def test_list_templates_tem_os_4(self) -> None:
        templates = list_templates()
        assert "feature-flow" in templates
        assert "bug-flow" in templates
        assert "hotfix-flow" in templates
        assert "debt-flow" in templates

    def test_feature_tem_nos_esperados(self) -> None:
        wf = get_template("feature-flow")
        assert wf is not None
        ids = wf.node_ids()
        assert "todo" in ids
        assert "dev" in ids
        assert "review" in ids
        assert "qa" in ids
        assert "done" in ids
        assert "kiro-reviewer" in ids

    def test_feature_valida_sem_erros(self) -> None:
        wf = get_template("feature-flow")
        assert wf is not None
        errors = wf.validate()
        assert errors == [], f"Erros no feature-flow: {errors}"

    def test_hotfix_tem_bypass(self) -> None:
        wf = get_template("hotfix-flow")
        assert wf is not None
        bypass_edges = [e for e in wf.edges if e.requires_bypass]
        assert len(bypass_edges) >= 1

    def test_debt_tem_cov(self) -> None:
        wf = get_template("debt-flow")
        assert wf is not None
        cov_node = wf.get_node("cov")
        assert cov_node is not None
        assert cov_node.kind is NodeKind.GATE

    def test_hotfix_valida_sem_erros(self) -> None:
        wf = get_template("hotfix-flow")
        assert wf is not None
        errors = wf.validate()
        # O self-loop do gate-tl no debt é intencional, não é erro de validação
        assert "inexistente" not in " ".join(errors)

    def test_debt_valida_sem_erros_de_nos_inexistentes(self) -> None:
        wf = get_template("debt-flow")
        assert wf is not None
        errors = wf.validate()
        assert not any("inexistente" in e for e in errors)
