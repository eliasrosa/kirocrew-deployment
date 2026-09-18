#!/usr/bin/env bash
# KiroCrew Flow — aplica as 19 labels do padrão `crewflow:*` num repo (idempotente via --force).
# Uso: ./scripts/setup-labels.sh owner/repo [owner/repo ...]
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
# Duas dimensões independentes:
#   ESTADO      — 1 por vez, ordem canônica spec > ready > todo > dev > review > qa > done
#   MODIFICADOR — 0..N, sobrepoem ao estado; os de parada tem prioridade
LABELS=(
  # ── ESTADOS (1 por vez) ──────────────────────────────────────────────
  "crewflow:spec|FEF3C7|PM especificando"
  "crewflow:ready|FBBF24|Especificacao pronta, aguardando priorizacao"
  "crewflow:todo|16A34A|Priorizado, aguardando dev pegar (GATILHO da esteira)"
  "crewflow:dev|2563EB|Em desenvolvimento"
  "crewflow:review|8B5CF6|PR aberto: review automatizado + aprovacao do TL (ANTES do QA)"
  "crewflow:qa|0EA5E9|Deploy HML manual + QA testa (DEPOIS do review)"
  "crewflow:done|22C55E|Concluido"

  # ── MODIFICADORES (0..N, sobrepoem) ──────────────────────────────────
  "crewflow:blocked|DC2626|Bloqueado - para tudo (tem prioridade sobre o estado)"
  "crewflow:running|F97316|Trabalho em andamento no estado atual"
  "crewflow:reviewed|6B7280|Lock anti-loop: ja analisado neste SHA"
  # Excecao auditada do fluxo de hotfix: pulou HML e foi direto pra PRD.
  # Exige justificativa no comentario da issue — o motor bloqueia o merge sem
  # ela. Existe pra tornar a excecao CONTAVEL: sem label, "quantos hotfixes
  # pularam HML neste trimestre?" nao tem resposta.
  "crewflow:hml-bypass|C2410C|Excecao auditada: hotfix foi direto pra PRD sem passar por HML (exige justificativa)"
  # Pedido de mudanca do reviewer: a issue volta pro dev para re-trabalho na MESMA PR.
  # Removida automaticamente quando o dev abre o novo commit (crewflow:reviewed some).
  "crewflow:changes-requested|9333EA|Reviewer pediu mudanca: dev deve corrigir e re-submeter na mesma PR"

  # ── TIPO DE FLUXO (routing: define qual workflow aplicar) ────────────
  "crewflow:feature|A855F7|Feature nova"
  "crewflow:bug|EF4444|Correcao de bug"
  "crewflow:hotfix|B91C1C|Correcao urgente em producao"
  "crewflow:debt|78716C|Debito tecnico"

  # ── PRIORIDADE ───────────────────────────────────────────────────────
  "crewflow:p1|B91C1C|Critico"
  "crewflow:p2|F59E0B|Alto"
  "crewflow:p3|3B82F6|Normal"
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
