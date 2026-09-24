#!/usr/bin/env bash
# Teste isolado do webhook do dashboard KiroCrew.
# Faz um POST assinado (HMAC-SHA256) que deve abrir uma sessão de agente visível.
#
# Uso: ./test-webhook.sh "mensagem para o agente"
#
# Lê token e secret de ~/.kiro/crew/crons/deployment.config.yaml
# (webhook_token = kc_whk_..., webhook_secret = kc_whs_...).
set -euo pipefail

CONFIG="$HOME/.kiro/crew/crons/deployment.config.yaml"
URL="http://localhost:5478/api/hooks/agent"

MSG="${1:-Sessão de teste do webhook. Responda apenas: webhook funcionando. Não faça mais nada.}"

# Extrai token e secret do config
TOKEN=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG'))['webhook_token'])")
SECRET=$(python3 -c "import yaml; print(yaml.safe_load(open('$CONFIG'))['webhook_secret'])")

if [ -z "$TOKEN" ] || [ -z "$SECRET" ]; then
  echo "ERRO: webhook_token ou webhook_secret vazio em $CONFIG" >&2
  exit 1
fi

# Corpo: sessionKey fixo e nomeado para a sessão aparecer identificável.
# deliver:true pede entrega no canal do dono.
SESSION_KEY="hook:teste-webhook-manual"
BODY=$(printf '{"message":%s,"sessionKey":"%s","name":"Teste Webhook","deliver":true}' \
  "$(python3 -c "import json,sys; print(json.dumps(sys.argv[1]))" "$MSG")" \
  "$SESSION_KEY")

TS=$(date +%s)
SIG=$(printf '%s.%s' "$TS" "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | sed 's/^.* //')

echo "→ POST $URL"
echo "→ sessionKey: $SESSION_KEY"
echo "→ mensagem: $MSG"
echo ""

curl -s -w "\nHTTP: %{http_code}\n" -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Origin: http://localhost:5478" \
  -H "X-KiroCrew-Timestamp: $TS" \
  -H "X-KiroCrew-Signature: sha256=$SIG" \
  -H "Content-Type: application/json" \
  --data-raw "$BODY"
