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


# ---------------------------------------------------------------------------
# RepoConfig — configuração por repositório (issue #245)
# ---------------------------------------------------------------------------

class TestRepoConfig:
    """Testes para RepoConfig e os métodos por-repo da SquadConfig."""

    def _squad_with_repo_configs(self) -> object:
        """Retorna um SquadConfig com dois repos configurados explicitamente."""
        from flow.config.squad import RepoConfig, SquadConfig, WorkflowParams

        sc = SquadConfig(
            id="test",
            name="Test Squad",
            issue_provider="github",
            projects=["org/api-gateway2", "org/api-subscription2"],
            repos=frozenset(["org/api-gateway2", "api-gateway2",
                             "org/api-subscription2", "api-subscription2"]),
            workflow_template="versao-c",
            workflow_params=WorkflowParams(auto_merge_on_approve=True),
            repo_configs=[
                RepoConfig(name="org/api-gateway2", auto_dispatch=True, auto_merge=False),
                RepoConfig(name="org/api-subscription2", auto_dispatch=False, auto_merge=True),
            ],
        )
        return sc

    def test_get_repo_config_exato(self) -> None:
        """get_repo_config retorna a config correta para nome exato."""
        sc: SquadConfig = self._squad_with_repo_configs()  # type: ignore[assignment]
        rc = sc.get_repo_config("org/api-gateway2")
        assert rc is not None
        assert rc.name == "org/api-gateway2"
        assert rc.auto_dispatch is True
        assert rc.auto_merge is False

    def test_get_repo_config_short_name(self) -> None:
        """get_repo_config casa pelo nome curto (sem org/)."""
        sc: SquadConfig = self._squad_with_repo_configs()  # type: ignore[assignment]
        rc = sc.get_repo_config("api-gateway2")  # sem prefixo org/
        assert rc is not None
        assert rc.auto_dispatch is True

    def test_get_repo_config_inexistente_retorna_none(self) -> None:
        """get_repo_config retorna None para repo sem config específica."""
        sc: SquadConfig = self._squad_with_repo_configs()  # type: ignore[assignment]
        assert sc.get_repo_config("org/outro-repo") is None

    def test_auto_dispatch_for_usa_config_repo(self) -> None:
        """auto_dispatch_for respeita a config por repo sobre o global."""
        sc: SquadConfig = self._squad_with_repo_configs()  # type: ignore[assignment]
        # gateway2 tem auto_dispatch=True, independente do global
        assert sc.auto_dispatch_for("org/api-gateway2", global_auto_dispatch=False) is True
        # subscription2 tem auto_dispatch=False, independente do global
        assert sc.auto_dispatch_for("org/api-subscription2", global_auto_dispatch=True) is False

    def test_auto_dispatch_for_fallback_global(self) -> None:
        """auto_dispatch_for usa o global quando repo não tem config específica."""
        sc: SquadConfig = self._squad_with_repo_configs()  # type: ignore[assignment]
        assert sc.auto_dispatch_for("org/outro-repo", global_auto_dispatch=True) is True
        assert sc.auto_dispatch_for("org/outro-repo", global_auto_dispatch=False) is False

    def test_auto_merge_for_usa_config_repo(self) -> None:
        """auto_merge_for respeita a config por repo sobre o global."""
        sc: SquadConfig = self._squad_with_repo_configs()  # type: ignore[assignment]
        # gateway2 tem auto_merge=False, mesmo que global=True
        assert sc.auto_merge_for("org/api-gateway2", global_auto_merge=True) is False
        # subscription2 tem auto_merge=True, mesmo que global=False
        assert sc.auto_merge_for("org/api-subscription2", global_auto_merge=False) is True

    def test_auto_merge_for_fallback_global(self) -> None:
        """auto_merge_for usa o global quando repo não tem config específica."""
        sc: SquadConfig = self._squad_with_repo_configs()  # type: ignore[assignment]
        assert sc.auto_merge_for("org/outro-repo", global_auto_merge=True) is True
        assert sc.auto_merge_for("org/outro-repo", global_auto_merge=False) is False

    def test_parse_squad_com_repos_config(self) -> None:
        """_parse_squad parseia corretamente repos_config do YAML."""
        from flow.config.squad import _parse_squad

        raw = {
            "id": "minha-squad",
            "issue_provider": "github",
            "repos": ["org/api-gateway2", "org/api-subscription2"],
            "repos_config": [
                {"name": "org/api-gateway2", "auto_dispatch": True, "auto_merge": False},
                {"name": "org/api-subscription2", "auto_dispatch": False},
            ],
        }
        sc = _parse_squad(raw)
        assert len(sc.repo_configs) == 2

        rc_gw = sc.get_repo_config("org/api-gateway2")
        assert rc_gw is not None
        assert rc_gw.auto_dispatch is True
        assert rc_gw.auto_merge is False

        rc_sub = sc.get_repo_config("org/api-subscription2")
        assert rc_sub is not None
        assert rc_sub.auto_dispatch is False
        assert rc_sub.auto_merge is None  # não especificado → None (fallback global)

    def test_parse_squad_sem_repos_config(self) -> None:
        """_parse_squad sem repos_config resulta em lista vazia."""
        from flow.config.squad import _parse_squad

        raw = {
            "id": "minha-squad",
            "issue_provider": "github",
            "repos": ["org/api-gateway2"],
        }
        sc = _parse_squad(raw)
        assert sc.repo_configs == []

    def test_load_squad_yaml_com_repos_config(self) -> None:
        """load_squad lê repos_config de arquivo YAML."""
        from flow.config.squad import load_squad

        yaml_content = """\
id: cogna-squad
issue_provider: github
repos:
  - org/api-gateway2
  - org/api-subscription2
repos_config:
  - name: org/api-gateway2
    auto_dispatch: true
    auto_merge: false
  - name: org/api-subscription2
    auto_dispatch: true
    auto_merge: true
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            tmp_path = f.name

        try:
            sc = load_squad(tmp_path)
            assert len(sc.repo_configs) == 2
            rc_gw = sc.get_repo_config("org/api-gateway2")
            assert rc_gw is not None
            assert rc_gw.auto_dispatch is True
            assert rc_gw.auto_merge is False
            rc_sub = sc.get_repo_config("org/api-subscription2")
            assert rc_sub is not None
            assert rc_sub.auto_merge is True
        finally:
            import os
            os.unlink(tmp_path)

    def test_repo_config_none_fields_sao_none(self) -> None:
        """RepoConfig com campos omitidos deve ter None (não False/True)."""
        from flow.config.squad import _parse_squad

        raw = {
            "id": "squad-x",
            "issue_provider": "github",
            "repos": ["org/repo-a"],
            "repos_config": [
                {"name": "org/repo-a"},  # sem auto_dispatch nem auto_merge
            ],
        }
        sc = _parse_squad(raw)
        rc = sc.get_repo_config("org/repo-a")
        assert rc is not None
        assert rc.auto_dispatch is None
        assert rc.auto_merge is None

    def test_auto_dispatch_for_sem_squad_usa_global(self) -> None:
        """auto_dispatch_for com squad=None e sem repo_configs usa o global."""
        from flow.config.squad import SquadConfig, WorkflowParams

        sc = SquadConfig(
            id="t",
            name="T",
            issue_provider="github",
            projects=["org/repo"],
            repos=frozenset(["org/repo", "repo"]),
            workflow_template="versao-c",
            workflow_params=WorkflowParams(),
            repo_configs=[],  # sem config por repo
        )
        # Sem repo_config específico, usa o global
        assert sc.auto_dispatch_for("org/repo", global_auto_dispatch=True) is True
        assert sc.auto_dispatch_for("org/repo", global_auto_dispatch=False) is False

    def test_auto_for_repo_com_squad_usa_repo_config(self) -> None:
        """auto_dispatch_for com SquadConfig e repo com config específica usa a do repo."""
        from flow.config.squad import RepoConfig, SquadConfig, WorkflowParams

        sc = SquadConfig(
            id="t",
            name="T",
            issue_provider="github",
            projects=["org/repo"],
            repos=frozenset(["org/repo", "repo"]),
            workflow_template="versao-c",
            workflow_params=WorkflowParams(),
            repo_configs=[RepoConfig(name="org/repo", auto_dispatch=True)],
        )
        # global=False mas repo tem auto_dispatch=True
        assert sc.auto_dispatch_for("org/repo", global_auto_dispatch=False) is True


# ---------------------------------------------------------------------------
# Config inline por repo em `repos:` + auto_dispatch/auto_merge (issue #264)
# ---------------------------------------------------------------------------

class TestRepoInlineConfig:
    """Testes para a forma inline `repos:` e os métodos auto_dispatch/auto_merge."""

    _GH = "https://github.com/eliasrosa/kirocrew-flow"
    _AZ = (
        "https://dev.azure.com/your-org/YourProject/_git/"
        "your-repo"
    )

    def test_parse_repos_inline_dict(self) -> None:
        """`repos:` com mapas url/auto_dispatch/auto_merge vira RepoConfig."""
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            "repos": [
                {"url": self._GH, "auto_dispatch": True, "auto_merge": True},
                {"url": self._AZ, "auto_dispatch": True, "auto_merge": False},
            ],
        }
        sc = _parse_squad(raw)
        assert len(sc.repo_configs) == 2

        rc_gh = sc.get_repo_config(self._GH)
        assert rc_gh is not None
        assert rc_gh.auto_dispatch is True
        assert rc_gh.auto_merge is True

        rc_az = sc.get_repo_config(self._AZ)
        assert rc_az is not None
        assert rc_az.auto_dispatch is True
        assert rc_az.auto_merge is False

    # Identificadores normalizados esperados (NÃO a url crua) — o scanner e o
    # provider do GitHub consomem `owner/repo`, não uma https URL (issue #264).
    _GH_ID = "eliasrosa/kirocrew-flow"
    _AZ_ID = "kdop/YourProject/your-repo"

    def test_projects_e_repos_derivados_do_url(self) -> None:
        """projects/repos derivam do 'url' NORMALIZADO das entradas dict.

        Regressão da issue #264: uma https URL crua em ``projects`` quebraria as
        chamadas ``gh api repos/{owner_repo}/...`` do scanner/provider. O parser
        deve normalizar github.com → owner/repo e dev.azure.com → org/proj/repo.
        """
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            "repos": [
                {"url": self._GH, "auto_dispatch": True},
                {"url": self._AZ},
            ],
        }
        sc = _parse_squad(raw)
        # projects contém o identificador owner/repo, NUNCA a url crua.
        assert self._GH_ID in sc.projects
        assert self._AZ_ID in sc.projects
        assert self._GH not in sc.projects
        assert self._AZ not in sc.projects
        # nenhuma entrada de projects começa com http (garantia anti-regressão)
        assert all(not p.lower().startswith("http") for p in sc.projects)
        # repos contém o identificador e o nome curto (validação de título)
        assert self._GH_ID in sc.repos
        assert "kirocrew-flow" in sc.repos
        assert "your-repo" in sc.repos

    def test_url_github_normalizada_para_owner_repo(self) -> None:
        """Uma url github completa vira owner/repo em projects e repo_configs."""
        raw = {
            "id": "gh",
            "issue_provider": "github",
            "repos": [{"url": self._GH, "auto_dispatch": True, "auto_merge": True}],
        }
        sc = _parse_squad(raw)
        assert sc.projects == [self._GH_ID]
        assert len(sc.repo_configs) == 1
        assert sc.repo_configs[0].name == self._GH_ID
        # get_repo_config resolve tanto pela url quanto pelo owner/repo.
        assert sc.get_repo_config(self._GH) is not None
        assert sc.get_repo_config(self._GH_ID) is not None

    def test_string_e_dict_misturados(self) -> None:
        """Entradas string (legado) e dict (inline) podem coexistir em `repos:`."""
        raw = {
            "id": "mix",
            "issue_provider": "github",
            "repos": [
                "org/repo-simples",
                {"url": self._GH, "auto_dispatch": True},
            ],
        }
        sc = _parse_squad(raw)
        assert "org/repo-simples" in sc.projects
        # url normalizada para owner/repo (não a url crua)
        assert self._GH_ID in sc.projects
        assert self._GH not in sc.projects
        # só o dict com flag gera RepoConfig
        assert len(sc.repo_configs) == 1
        assert sc.get_repo_config("org/repo-simples") is None
        assert sc.get_repo_config(self._GH) is not None

    def test_auto_dispatch_metodo_usa_config_repo(self) -> None:
        """auto_dispatch(repo_url) retorna o valor por repo quando definido."""
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            "repos": [
                {"url": self._GH, "auto_dispatch": True},
                {"url": self._AZ, "auto_dispatch": False},
            ],
        }
        sc = _parse_squad(raw)
        # global armazenado é False, mas o repo GH sobrepõe para True
        sc.global_auto_dispatch = False
        assert sc.auto_dispatch(self._GH) is True
        assert sc.auto_dispatch(self._AZ) is False

    def test_auto_merge_metodo_usa_config_repo(self) -> None:
        """auto_merge(repo_url) retorna o valor por repo quando definido."""
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            "repos": [
                {"url": self._GH, "auto_merge": True},
                {"url": self._AZ, "auto_merge": False},
            ],
        }
        sc = _parse_squad(raw)
        sc.global_auto_merge = True  # global True, mas AZ sobrepõe para False
        assert sc.auto_merge(self._GH) is True
        assert sc.auto_merge(self._AZ) is False

    def test_auto_dispatch_metodo_fallback_global(self) -> None:
        """auto_dispatch(repo_url) cai no global quando o repo não define o flag."""
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            # url sem auto_dispatch → herda o global
            "repos": [{"url": self._GH, "auto_merge": True}],
        }
        sc = _parse_squad(raw)
        sc.global_auto_dispatch = True
        assert sc.auto_dispatch(self._GH) is True
        sc.global_auto_dispatch = False
        assert sc.auto_dispatch(self._GH) is False

    def test_auto_merge_metodo_fallback_global(self) -> None:
        """auto_merge(repo_url) cai no global quando o repo não define o flag."""
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            "repos": [{"url": self._GH, "auto_dispatch": True}],
        }
        sc = _parse_squad(raw)
        sc.global_auto_merge = True
        assert sc.auto_merge(self._GH) is True
        sc.global_auto_merge = False
        assert sc.auto_merge(self._GH) is False

    def test_metodos_fallback_global_repo_desconhecido(self) -> None:
        """Repo sem config alguma resolve totalmente pelo global armazenado."""
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            "repos": [{"url": self._GH, "auto_dispatch": True, "auto_merge": True}],
        }
        sc = _parse_squad(raw)
        sc.global_auto_dispatch = True
        sc.global_auto_merge = False
        assert sc.auto_dispatch("org/outro-repo") is True
        assert sc.auto_merge("org/outro-repo") is False

    def test_precedencia_inline_sobre_repos_config(self) -> None:
        """Se o repo aparece em `repos:` e `repos_config:`, o inline vence."""
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            "repos": [
                {"url": self._GH, "auto_dispatch": True, "auto_merge": True},
            ],
            "repos_config": [
                # mesmo repo (nome curto casa) com valores opostos
                {"name": "eliasrosa/kirocrew-flow", "auto_dispatch": False, "auto_merge": False},
            ],
        }
        sc = _parse_squad(raw)
        # só uma RepoConfig (a inline), sem duplicata
        assert len(sc.repo_configs) == 1
        rc = sc.get_repo_config(self._GH)
        assert rc is not None
        assert rc.auto_dispatch is True
        assert rc.auto_merge is True

    def test_repos_mesmo_nome_curto_orgs_diferentes_nao_colidem(self) -> None:
        """Repos com mesmo nome curto em orgs diferentes NÃO colapsam (issue #264).

        ``orgA/service`` e ``orgB/service`` compartilham o último segmento, mas
        são repos distintos. Cada um deve manter sua própria política.
        """
        raw = {
            "id": "multi-org",
            "issue_provider": "github",
            "repos": [
                {"url": "https://github.com/orgA/service", "auto_merge": True},
                {"url": "https://github.com/orgB/service", "auto_merge": False},
            ],
        }
        sc = _parse_squad(raw)
        # Duas configs distintas — não deduplicadas pelo nome curto "service".
        assert len(sc.repo_configs) == 2
        rc_a = sc.get_repo_config("orgA/service")
        rc_b = sc.get_repo_config("orgB/service")
        assert rc_a is not None and rc_a.auto_merge is True
        assert rc_b is not None and rc_b.auto_merge is False

    def test_merge_nao_colapsa_inline_e_legacy_de_orgs_diferentes(self) -> None:
        """Inline e legacy com mesmo nome curto em orgs diferentes coexistem."""
        raw = {
            "id": "multi-org-merge",
            "issue_provider": "github",
            "repos": [
                {"url": "https://github.com/orgA/service", "auto_dispatch": True},
            ],
            "repos_config": [
                {"name": "orgB/service", "auto_dispatch": False},
            ],
        }
        sc = _parse_squad(raw)
        assert len(sc.repo_configs) == 2
        assert sc.get_repo_config("orgA/service").auto_dispatch is True  # type: ignore[union-attr]
        assert sc.get_repo_config("orgB/service").auto_dispatch is False  # type: ignore[union-attr]

    def test_get_repo_config_short_name_compat_245(self) -> None:
        """Compat #245: consultar pelo nome curto casa com a entrada org/repo."""
        raw = {
            "id": "compat",
            "issue_provider": "github",
            "repos": ["eliasrosa/kirocrew-flow"],
            "repos_config": [
                {"name": "eliasrosa/kirocrew-flow", "auto_dispatch": True},
            ],
        }
        sc = _parse_squad(raw)
        # nome curto casa (um lado sem org/host)
        rc = sc.get_repo_config("kirocrew-flow")
        assert rc is not None and rc.auto_dispatch is True
        # url completa também resolve para a mesma entrada
        assert sc.get_repo_config(self._GH) is not None

    def test_get_repo_config_nao_casa_org_diferente(self) -> None:
        """Consultar org diferente com mesmo nome curto NÃO casa (issue #264)."""
        raw = {
            "id": "iso",
            "issue_provider": "github",
            "repos": ["orgB/service"],
            "repos_config": [
                {"name": "orgB/service", "auto_merge": True},
            ],
        }
        sc = _parse_squad(raw)
        # ambos carregam org → não casa por nome curto
        assert sc.get_repo_config("orgA/service") is None
        # o próprio org resolve
        assert sc.get_repo_config("orgB/service") is not None

    def test_url_ausente_em_dict_falha(self) -> None:
        """Entrada dict em `repos:` sem 'url' falha alto."""
        raw = {
            "id": "cogna",
            "issue_provider": "github",
            "repos": [{"auto_dispatch": True}],
        }
        with pytest.raises(SquadConfigError):
            _parse_squad(raw)

    def test_load_squad_yaml_repos_inline(self) -> None:
        """load_squad lê a forma inline `repos:` de um arquivo YAML real (PyYAML)."""
        yaml_content = """\
id: cogna-squad
issue_provider: github
repos:
  - url: https://github.com/eliasrosa/kirocrew-flow
    auto_dispatch: true
    auto_merge: true
  - url: https://dev.azure.com/your-org/YourProject/_git/your-repo
    auto_dispatch: true
    auto_merge: false
"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(yaml_content)
            tmp_path = f.name

        try:
            sc = load_squad(tmp_path)
            sc.global_auto_dispatch = False
            sc.global_auto_merge = False
            gh = "https://github.com/eliasrosa/kirocrew-flow"
            az = (
                "https://dev.azure.com/your-org/YourProject/_git/"
                "your-repo"
            )
            # projects derivam do url NORMALIZADO (owner/repo, não a url crua)
            assert "eliasrosa/kirocrew-flow" in sc.projects
            assert "kdop/YourProject/your-repo" in sc.projects
            assert gh not in sc.projects
            assert az not in sc.projects
            # resolução por repo com fallback global (aceita url OU owner/repo)
            assert sc.auto_dispatch(gh) is True
            assert sc.auto_merge(gh) is True
            assert sc.auto_dispatch(az) is True
            assert sc.auto_merge(az) is False
            # resolve igual quando passamos o identificador normalizado
            assert sc.auto_merge("eliasrosa/kirocrew-flow") is True
            assert sc.auto_merge("kdop/YourProject/your-repo") is False
            # repo desconhecido cai no global armazenado
            assert sc.auto_dispatch("org/desconhecido") is False
            assert sc.auto_merge("org/desconhecido") is False
        finally:
            import os
            os.unlink(tmp_path)
