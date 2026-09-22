#!/usr/bin/env bash
# KiroCrew Flow — instala os scripts de cron no Kiro Crew.
#
# Uso: ./scripts/install-cron.sh
#
# O que faz:
#   1. Copia deployment/deployment.py para ~/.kiro/crew/crons/
#   2. Aplica o patch de sys.path para que flow/ seja importável
#      (o script é executado de ~/.kiro/crew/crons/, não do repo)
#   3. Copia deployment/deployment.config.yaml se não existir ainda
#   4. Copia scripts/flow_auto_update.py para ~/.kiro/crew/crons/
#
# Os crons são registrados automaticamente via app.json ao instalar/habilitar
# o App no Kiro Crew (kirocrew app enable kirocrew-flow).
# Para registrar manualmente (cron legado):
#   cron_add(name="crewflow-scan",
#            script="~/.kiro/crew/crons/deployment.py:run",
#            every=600)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CRONS_DIR="$HOME/.kiro/crew/crons"

echo "=== KiroCrew Flow — instalando cron ==="
echo "  repo:  $REPO_ROOT"
echo "  crons: $CRONS_DIR"

mkdir -p "$CRONS_DIR"

# 1. Copia o script monolítico
cp "$REPO_ROOT/deployment/deployment.py" "$CRONS_DIR/deployment.py"
echo "  ✅ deployment.py copiado"

# 1b. Copia a pasta deployment/flow/ (módulos por cron)
mkdir -p "$CRONS_DIR/deployment/flow"
cp "$REPO_ROOT/deployment/flow/__init__.py" "$CRONS_DIR/deployment/__init__.py" 2>/dev/null || true
cp "$REPO_ROOT/deployment/flow/__init__.py" "$CRONS_DIR/deployment/flow/__init__.py"
for f in base dev reviewer merge review_approved qa_approved conflict rework qa_notify qa_refused; do
    if [ -f "$REPO_ROOT/deployment/flow/${f}.py" ]; then
        cp "$REPO_ROOT/deployment/flow/${f}.py" "$CRONS_DIR/deployment/flow/${f}.py"
    fi
done
echo "  ✅ deployment/flow/ copiado"

# 2. Aplica o patch de sys.path
# O script instalado roda de ~/.kiro/crew/crons/ onde flow/ não existe.
# Precisamos injetar o caminho real do repo no sys.path.
python3 - <<PYEOF
import os, sys

path = os.path.join(os.path.expanduser("~"), ".kiro/crew/crons/deployment.py")
repo_root = "$REPO_ROOT"

with open(path) as f:
    content = f.read()

OLD = "_REPO_ROOT = os.path.dirname(_HERE)\nif _REPO_ROOT not in sys.path:\n    sys.path.insert(0, _REPO_ROOT)"
NEW = f"""_REPO_ROOT = os.path.dirname(_HERE)
# Quando instalado em ~/.kiro/crew/crons/, _REPO_ROOT aponta para ~/.kiro/crew/
# onde flow/ não existe. Adicionamos o caminho real do repo:
_FLOW_ROOT = "{repo_root}"
if _FLOW_ROOT not in sys.path:
    sys.path.insert(0, _FLOW_ROOT)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)"""

if "_FLOW_ROOT" in content:
    print("  ✅ patch sys.path já aplicado")
elif OLD in content:
    with open(path, "w") as f:
        f.write(content.replace(OLD, NEW))
    print("  ✅ patch sys.path aplicado")
else:
    print("  ⚠️  padrão sys.path não encontrado — verifique manualmente")
    sys.exit(1)
PYEOF

# 3. Copia a config se não existir
CONFIG_SRC="$REPO_ROOT/deployment/deployment.config.yaml"
CONFIG_DST="$CRONS_DIR/deployment.config.yaml"
if [ -f "$CONFIG_DST" ]; then
    echo "  ℹ️  deployment.config.yaml já existe (não sobrescrito)"
else
    cp "$CONFIG_SRC" "$CONFIG_DST"
    echo "  ✅ deployment.config.yaml copiado"
    echo "  ⚠️  Edite $CONFIG_DST:"
    echo "       squad_config: /caminho/absoluto/para/squads/minha-squad.yaml"
    echo "       notify_chat_id: <seu chat_id>"
    echo "       dev_root: /caminho/para/seus/clones"
fi

# 4. Copia o script de auto-update
cp "$REPO_ROOT/scripts/flow_auto_update.py" "$CRONS_DIR/flow_auto_update.py"
echo "  ✅ flow_auto_update.py copiado"

# 5. Grava hash de versão para detecção de script desatualizado
# deployment.py verifica este arquivo no startup e avisa quando diverge do repo.
VERSION_FILE="$CRONS_DIR/deployment.version"
python3 - <<PYEOF
import hashlib, json, os

repo_root = "$REPO_ROOT"
crons_dir = "$CRONS_DIR"

# Hash do deployment.py do REPO (antes do patch de sys.path)
src = os.path.join(repo_root, "deployment", "deployment.py")
with open(src, "rb") as f:
    repo_hash = hashlib.sha256(f.read()).hexdigest()

version_info = {
    "repo_root": repo_root,
    "repo_deployment_sha256": repo_hash,
}

version_path = os.path.join(crons_dir, "deployment.version")
with open(version_path, "w") as f:
    json.dump(version_info, f, indent=2)

print(f"  ✅ deployment.version gravado (sha256: {repo_hash[:12]}...)")
PYEOF

echo ""
echo "=== Instalação completa ==="
echo ""
echo "  Para registrar os crons por estágio (recomendado), no dashboard do Kiro Crew:"
echo ""
echo "    # Cron dev — implementação (issues flow:develop-waiting)"
echo "    cron_add(name=\"flow-dev\","
echo "             script=\"~/.kiro/crew/crons/deployment/flow/dev.py:run\","
echo "             every=600)"
echo ""
echo "    # Cron reviewer — code review (PRs flow:review-waiting)"
echo "    cron_add(name=\"flow-reviewer\","
echo "             script=\"~/.kiro/crew/crons/deployment/flow/reviewer.py:run\","
echo "             every=300)"
echo ""
echo "    # Cron merge — merge squash (flow:review-approved / flow:qa-approved)"
echo "    # DEPRECIADO: prefira os dois crons separados abaixo"
echo "    cron_add(name=\"flow-merge\","
echo "             script=\"~/.kiro/crew/crons/deployment/flow/merge.py:run\","
echo "             every=120)"
echo ""
echo "    # Cron review-approved — merge squash após review (flow:review-approved → flow:qa-waiting)"
echo "    cron_add(name=\"flow-review-approved\","
echo "             script=\"~/.kiro/crew/crons/deployment/flow/review_approved.py:run\","
echo "             every=120)"
echo ""
echo "    # Cron qa-approved — merge squash final após QA (flow:qa-approved → flow:done)"
echo "    cron_add(name=\"flow-qa-approved\","
echo "             script=\"~/.kiro/crew/crons/deployment/flow/qa_approved.py:run\","
echo "             every=120)"
echo ""
echo "    # Cron conflito/rework — resolução de conflito e re-trabalho pós-review"
echo "    cron_add(name=\"flow-rework\","
echo "             script=\"~/.kiro/crew/crons/deployment/flow/rework.py:run\","
echo "             every=300)"
echo ""
echo "  Ou, usando os entrypoints legados (equivalentes, também funcionam):"
echo "    script=\"~/.kiro/crew/crons/deployment.py:run_dev\""
echo "    script=\"~/.kiro/crew/crons/deployment.py:run_reviewer\""
echo "    script=\"~/.kiro/crew/crons/deployment.py:run_review_approved\""
echo "    script=\"~/.kiro/crew/crons/deployment.py:run_qa_approved\""
echo "    script=\"~/.kiro/crew/crons/deployment.py:run_merge\"  # depreciado: processa ambos os estados"
echo "    script=\"~/.kiro/crew/crons/deployment.py:run_conflito\""
echo ""
echo "  Ou, para usar o cron monolítico legado (todos os estágios em sequência):"
echo "    cron_add(name=\"crewflow-scan\","
echo "             script=\"~/.kiro/crew/crons/deployment.py:run\","
echo "             every=600)"
