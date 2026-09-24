#!/usr/bin/env bash
# KiroCrew Flow — aplica as labels do namespace `flow:` num repo (idempotente via --force).
# Uso: ./scripts/setup-flow-labels.sh owner/repo [owner/repo ...]
#
# 15 labels de estado + 3 modificadores transversais.
# Tipo e prioridade ficam no Jira/GitHub nativo — não são labels de fluxo.
#
# Para migrar de crewflow:* para flow:*, use o script migrate-labels.sh (TODO).
set -u

if [ "$#" -lt 1 ]; then
  echo "uso: $0 owner/repo [owner/repo ...]" >&2
  exit 1
fi

# nome|cor(hex sem #)|descricao
#
# Duas dimensões independentes:
#   ESTADO      — 1 por vez, ordem canônica do fluxo
#   MODIFICADOR — 0..N, sobrepõem ao estado; os de parada têm prioridade
LABELS=(
  # ── ESTADOS HUMANOS — briefing e planning ────────────────────────────
  "flow:briefing|FBBF24|TL/PM criou demanda e fez briefing com o dev"
  "flow:planning-specs|F59E0B|Dev montando spec, criterios de aceite e sub-tasks"
  "flow:planning-review|D97706|Dev pediu revisao ao TL/PM antes de implementar"

  # ── ESTADOS DO AGENTE — desenvolvimento ──────────────────────────────
  # GATILHO da esteira: cron-develop despacha sessao quando encontra esta label
  "flow:develop-waiting|3B82F6|Spec aprovada, aguardando agente pegar para implementar"
  "flow:develop-running|1D4ED8|Agente implementando (branch + PR aberta)"

  # ── ESTADOS DO AGENTE — code review ──────────────────────────────────
  "flow:review-waiting|7C3AED|PR aberta, aguardando reviewer automatizado"
  "flow:review-approved|6D28D9|Reviewer aprovou, pronto para QA"
  # Estado de espera HUMANA: TL/dev avalia o motivo e move manualmente
  "flow:review-refused|9333EA|Reviewer reprovou - aguarda TL/dev avaliar e mover label"

  # ── ESTADOS HUMANOS — QA ─────────────────────────────────────────────
  "flow:qa-waiting|0D9488|Deploy feito, aguardando QA pegar para testar"
  "flow:qa-testing|059669|QA testando ativamente em HML"
  # GATILHO: cron-qa-merge faz merge final quando encontra esta label
  "flow:qa-approved|16A34A|QA aprovou, cron faz merge final"
  # Estado de espera HUMANA: TL/dev decide se e rework direto ou novo ciclo
  "flow:qa-refused|15803D|QA reprovou - aguarda TL/dev avaliar e mover label"

  # ── ESTADO FINAL ──────────────────────────────────────────────────────
  "flow:done|22C55E|Concluido e mergeado"

  # ── MODIFICADORES (0..N, sobrepõem ao estado) ────────────────────────
  # Parada total: tem prioridade sobre qualquer estado
  "flow:blocked|DC2626|Bloqueado em qualquer estagio - para tudo"
  # Cron de conflito resolve via rebase e remove esta label
  "flow:merge-conflict|F97316|PR com conflito de merge - cron resolve via rebase"
  # Lock interno: reviewer em andamento ou resultado pendente (anti-loop por SHA)
  "flow:review-running|7C3AED|Lock interno: reviewer em andamento ou resultado pendente"
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
