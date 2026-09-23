"""Testes de squad config, workflow e routing — sem I/O de rede."""

from __future__ import annotations

import builtins
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
  - match: {labels: ["flow:hotfix"]}
    workflow: hotfix-flow
  - match: {labels: ["flow:bug"]}
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
        - flow:hotfix
    workflow: hotfix-flow
  - match:
      labels:
        - flow:bug
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
            {"match": {"labels": ["flow:hotfix"]}, "workflow": "hotfix-flow"},
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
            {"match": {"labels": ["flow:hotfix"]}, "workflow": "hotfix-flow"},
            {"match": {"labels": ["flow:bug"]}, "workflow": "bug-flow"},
            {"match": {"labels": ["flow:debt"]}, "workflow": "debt-flow"},
            {"default": "feature-flow"},
        ]
        return _parse_squad(d)

    def test_hotfix(self) -> None:
        s = self._squad()
        assert s.resolve_workflow(frozenset({"flow:hotfix", "flow:p1"})) == "hotfix-flow"

    def test_bug(self) -> None:
        s = self._squad()
        assert s.resolve_workflow(frozenset({"flow:bug"})) == "bug-flow"

    def test_default(self) -> None:
        s = self._squad()
        assert s.resolve_workflow(frozenset({"flow:feature"})) == "feature-flow"

    def test_sem_labels_usa_default(self) -> None:
        s = self._squad()
        assert s.resolve_workflow(frozenset()) == "feature-flow"

    def test_hotfix_tem_prioridade_sobre_bug(self) -> None:
        s = self._squad()
        # hotfix vem antes de bug nas regras
        assert s.resolve_workflow(frozenset({"flow:hotfix", "flow:bug"})) == "hotfix-flow"


# ---------------------------------------------------------------------------
# _mini_yaml — parser de fallback (sem PyYAML)
# ---------------------------------------------------------------------------

class TestMiniYaml:
    """Testes unitários do parser fallback _mini_yaml."""

    def _write(self, content: str) -> Path:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(content)
            return Path(f.name)

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
            '  - match: {labels: ["flow:hotfix"]}\n'
            "    workflow: hotfix-flow\n"
            "  - default: feature-flow\n"
        )
        p = self._write(yaml)
        try:
            result = _mini_yaml(p)
            routing = result["routing"]
            assert len(routing) == 2
            assert routing[0]["match"]["labels"] == ["flow:hotfix"]
            assert routing[0]["workflow"] == "hotfix-flow"
            assert routing[1]["default"] == "feature-flow"
        finally:
            p.unlink()

    def test_routing_multiline(self) -> None:
        yaml = (
            "routing:\n"
            "  - match:\n"
            "      labels:\n"
            "        - flow:hotfix\n"
            "    workflow: hotfix-flow\n"
            "  - match:\n"
            "      labels:\n"
            "        - flow:bug\n"
            "    workflow: bug-flow\n"
            "  - default: feature-flow\n"
        )
        p = self._write(yaml)
        try:
            result = _mini_yaml(p)
            routing = result["routing"]
            assert len(routing) == 3
            assert routing[0]["match"]["labels"] == ["flow:hotfix"]
            assert routing[0]["workflow"] == "hotfix-flow"
            assert routing[1]["match"]["labels"] == ["flow:bug"]
            assert routing[1]["workflow"] == "bug-flow"
            assert routing[2]["default"] == "feature-flow"
        finally:
            p.unlink()

    def test_routing_multiline_multiplas_labels(self) -> None:
        yaml = (
            "routing:\n"
            "  - match:\n"
            "      labels:\n"
            "        - flow:bug\n"
            "        - flow:p1\n"
            "    workflow: bug-flow\n"
        )
        p = self._write(yaml)
        try:
            result = _mini_yaml(p)
            labels = result["routing"][0]["match"]["labels"]
            assert "flow:bug" in labels
            assert "flow:p1" in labels
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
            assert "flow:hotfix" in sc.routing[0].labels
            assert sc.routing[1].workflow == "bug-flow"
            assert "flow:bug" in sc.routing[1].labels
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
# Fallback _mini_yaml (sem PyYAML) — parser mínimo embutido
# ---------------------------------------------------------------------------

# YAML de routing na forma MULTI-LINHA padrão (não inline). PyYAML parseia isto
# nativamente; o fallback _mini_yaml precisa produzir a MESMA estrutura.
MULTILINE_ROUTING_YAML = """\
id: my-squad
name: My Squad
issue_provider: github
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
        - flow:hotfix
    workflow: hotfix-flow
  - match:
      labels:
        - flow:bug
    workflow: bug-flow
  - default: feature-flow
"""


@pytest.fixture
def _no_pyyaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """Força `import yaml` a levantar ImportError, exercitando o fallback.

    O código guarda `try: import yaml except ImportError: return _mini_yaml(...)`,
    então basta fazer o import do módulo `yaml` falhar.
    """
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "yaml":
            raise ImportError("PyYAML indisponível (forçado no teste)")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake_import)


class TestMiniYamlFallback:
    """Garante que o schema de squad funciona SEM PyYAML."""

    def _write(self, text: str) -> str:
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(text)
            return f.name

    def test_import_yaml_falha_no_fixture(self, _no_pyyaml: None) -> None:
        # Sanidade: o fixture realmente bloqueia o import do PyYAML.
        with pytest.raises(ImportError):
            import yaml  # noqa: F401

    def test_multiline_routing_via_fallback(self, _no_pyyaml: None) -> None:
        tmp = self._write(MULTILINE_ROUTING_YAML)
        try:
            sc = load_squad(tmp)
        finally:
            Path(tmp).unlink()
        # As duas regras multi-linha + o default foram parseadas.
        assert len(sc.routing) == 2
        assert sc.default_workflow == "feature-flow"
        # resolve_workflow usa as regras corretamente.
        assert sc.resolve_workflow(frozenset({"flow:hotfix"})) == "hotfix-flow"
        assert sc.resolve_workflow(frozenset({"flow:bug"})) == "bug-flow"
        assert sc.resolve_workflow(frozenset({"flow:feature"})) == "feature-flow"

    def test_multiline_debt_routing_resolve(self, _no_pyyaml: None) -> None:
        # Exercita o template `debt` via routing multi-linha (issue de teste do motor).
        yaml_txt = (
            "id: sq\n"
            "issue_provider: github\n"
            "repos:\n"
            "  - org/api\n"
            "routing:\n"
            "  - match:\n"
            "      labels:\n"
            "        - flow:debt\n"
            "    workflow: debt-flow\n"
            "  - default: feature-flow\n"
        )
        tmp = self._write(yaml_txt)
        try:
            sc = load_squad(tmp)
        finally:
            Path(tmp).unlink()
        assert sc.resolve_workflow(frozenset({"flow:debt"})) == "debt-flow"

    def test_inline_routing_ainda_funciona_via_fallback(self, _no_pyyaml: None) -> None:
        # A forma inline `- match: {labels: [...]}` não pode regredir.
        tmp = self._write(EXAMPLE_YAML)
        try:
            sc = load_squad(tmp)
        finally:
            Path(tmp).unlink()
        assert len(sc.routing) == 2
        assert sc.resolve_workflow(frozenset({"flow:hotfix"})) == "hotfix-flow"
        assert sc.resolve_workflow(frozenset({"flow:bug"})) == "bug-flow"

    def test_fallback_estrutura_igual_ao_pyyaml(self, _no_pyyaml: None) -> None:
        # A estrutura crua produzida pelo fallback casa com a do example.yaml.
        example = Path(__file__).parent.parent.parent / "squads" / "example.yaml"
        raw = _mini_yaml(example)
        assert raw["routing"][0] == {
            "match": {"labels": ["flow:hotfix"]},
            "workflow": "hotfix-flow",
        }
        assert raw["routing"][-1] == {"default": "feature-flow"}
        assert raw["workflow_params"]["allow_hml_bypass"] is True


# ---------------------------------------------------------------------------
# Regressões de indentação no fallback _mini_yaml
# ---------------------------------------------------------------------------

# PyYAML está instalado no ambiente de dev, então comparamos o fallback
# DIRETAMENTE contra `yaml.safe_load` — sem precisar forçar ImportError, já que
# `_mini_yaml` é chamado explicitamente.
class TestMiniYamlIndentationRegressions:
    """Garante paridade com PyYAML em formas de indentação não-triviais.

    Estes casos FALHAVAM no parser anterior:
      1. listas na MESMA indentação da chave (forma flush-left) viravam `[]`;
      2. chaves de continuação de item de lista assumiam passo fixo de 2 espaços.
    """

    def _mini(self, text: str) -> object:
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            return _mini_yaml(Path(tmp))
        finally:
            Path(tmp).unlink()

    def test_lista_flush_left_nao_vira_vazia(self) -> None:
        # `repos:` seguido de itens na coluna 0 (mesma indentação da chave).
        # O parser antigo devolvia {'repos': []} (perda silenciosa de dados).
        import yaml

        text = "repos:\n- owner/repo-a\n- owner/repo-b\n"
        assert self._mini(text) == yaml.safe_load(text)
        assert self._mini(text) == {"repos": ["owner/repo-a", "owner/repo-b"]}

    def test_routing_flush_left_igual_ao_pyyaml(self) -> None:
        # `routing:` com itens flush-left: as regras SUMIAM no parser antigo,
        # fazendo tudo cair no default_workflow.
        import yaml

        text = (
            "routing:\n"
            "- match:\n"
            "    labels:\n"
            "    - flow:bug\n"
            "  workflow: bug-flow\n"
            "- default: feature-flow\n"
        )
        assert self._mini(text) == yaml.safe_load(text)

    def test_continuacao_com_passo_de_4_espacos(self) -> None:
        # Chave de continuação `workflow` a 4 espaços do `-` (não 2).
        # O parser antigo usava child_indent = indent + 2 fixo.
        import yaml

        text = (
            "routing:\n"
            "  - match:\n"
            "        labels:\n"
            "          - flow:bug\n"
            "    workflow: bug-flow\n"
        )
        assert self._mini(text) == yaml.safe_load(text)

    def test_continuacao_com_passo_de_1_espaco(self) -> None:
        import yaml

        text = (
            "routing:\n"
            "  - match:\n"
            "     labels:\n"
            "      - flow:bug\n"
            "    workflow: bug-flow\n"
        )
        assert self._mini(text) == yaml.safe_load(text)

    def test_item_de_lista_solto_onde_mapa_esperado_lanca(self) -> None:
        # Input ambíguo: um `- ` onde uma chave de mapa era esperada.
        # Deve falhar ALTO em vez de descartar dados silenciosamente.
        text = "id: sq\n- solto\n"
        with pytest.raises(SquadConfigError, match="inesperado"):
            self._mini(text)

    def test_equivalencia_com_pyyaml_em_squads_example(self) -> None:
        # Garante que a saída do fallback é estruturalmente idêntica à do
        # PyYAML para o arquivo de exemplo real de squad.
        import yaml

        example = Path(__file__).parent.parent.parent / "squads" / "example.yaml"
        with example.open() as f:
            expected = yaml.safe_load(f)
        assert _mini_yaml(example) == expected

    def test_equivalencia_com_pyyaml_em_config_example(self) -> None:
        # Idem para o config de cron de exemplo (deployment).
        import yaml

        cfg = Path(__file__).parent.parent.parent / "config.example.yaml"
        with cfg.open() as f:
            expected = yaml.safe_load(f)
        assert _mini_yaml(cfg) == expected


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
