#!/usr/bin/env bash
# KiroCrew Flow — aplica as labels de metadado `crewflow:*` num repo (idempotente via --force).
# Uso: ./scripts/setup-labels.sh owner/repo [owner/repo ...]
#
# Labels de ESTADO (ex: crewflow:todo, crewflow:dev) foram deprecadas — o estado
# agora vive no namespace flow:* (setup-flow-labels.sh). Aqui ficam apenas as labels
# de metadado (tipo de fluxo, prioridade e o modificador crewflow:blocked).
#
# O prefixo `crewflow:` funciona em GitHub e Jira. Confluence NÃO aceita `:`
# em label (só alfanumérico) — está fora de escopo.
set -u

if [ "$#" -lt 1 ]; then
  echo "uso: $0 owner/repo [owner/repo ...]" >&2
  exit 1
fi

# nome|cor(hex sem #)|descricao
#
# Metadado em duas dimensões:
#   TIPO        — define o workflow aplicado pelo motor (routing:)
#   PRIORIDADE  — sinalização de urgência
#   MODIFICADOR — crewflow:blocked permanece aqui por compatibilidade com
#                 regras externas que já o utilizam; o motor também reconhece
#                 flow:blocked (namespace novo).
LABELS=(
  # ── TIPO DE FLUXO (routing: define qual workflow aplicar) ────────────
  "crewflow:feature|A855F7|Feature nova"
  "crewflow:bug|EF4444|Correcao de bug"
  "crewflow:hotfix|B91C1C|Correcao urgente em producao"
  "crewflow:debt|78716C|Debito tecnico"

  # ── PRIORIDADE ───────────────────────────────────────────────────────
  "crewflow:p1|B91C1C|Critico"
  "crewflow:p2|F59E0B|Alto"
  "crewflow:p3|3B82F6|Normal"

  # ── MODIFICADOR DE PARADA (compatibilidade) ──────────────────────────
  # crewflow:blocked é reconhecido pelo motor ao lado de flow:blocked.
  # Manter aqui para repos que ainda usam o namespace legado.
  "crewflow:blocked|DC2626|Bloqueado - para tudo (tem prioridade sobre o estado)"
)

for repo in "$@"; do
  echo "== $repo =="
  for entry in "${LABELS[@]}"; do
    IFS='|' read -r name color desc <<< "$entry"
    if gh label create "$name" --repo "$repo" --color "$color" --description "$desc" --force >/dev/null 2>&1; then
      echo "  ok: $name"
    else
      echo "  FALHOU: $name"
    fi
  done
done
echo "== done =="
