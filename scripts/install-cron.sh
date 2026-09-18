#!/usr/bin/env bash
# KiroCrew Flow — instala o cron de scan no Kiro Crew.
#
# Uso: ./scripts/install-cron.sh
#
# O que faz:
#   1. Copia deployment/deployment.py para ~/.kiro/crew/crons/
#   2. Aplica o patch de sys.path para que flow/ seja importável
#      (o script é executado de ~/.kiro/crew/crons/, não do repo)
#   3. Copia deployment/deployment.config.yaml se não existir ainda
#
# Depois de instalar, registre o cron no Kiro Crew (uma vez):
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

# 1. Copia o script
cp "$REPO_ROOT/deployment/deployment.py" "$CRONS_DIR/deployment.py"
echo "  ✅ deployment.py copiado"

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

echo ""
echo "=== Instalação completa ==="
echo ""
echo "  Para registrar o cron (uma vez), no dashboard do Kiro Crew:"
echo "    cron_add(name=\"crewflow-scan\","
echo "             script=\"~/.kiro/crew/crons/deployment.py:run\","
echo "             every=600)"
