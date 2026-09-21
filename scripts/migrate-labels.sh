#!/usr/bin/env bash
# migrate-labels.sh — migra issues de crewflow:* para flow:* num repo.
#
# O que faz (por etapa):
#   1. Cria as labels flow:* no repo (idempotente, chama setup-flow-labels.sh)
#   2. Para cada issue aberta com label crewflow:*, troca para o equivalente flow:*
#   3. Labels sem equivalente (crewflow:reviewed, crewflow:changes-requested,
#      crewflow:running, crewflow:hml-bypass, crewflow:feature, crewflow:bug,
#      crewflow:hotfix, crewflow:debt, crewflow:p1/p2/p3) são removidas sem
#      substituto — tipo e prioridade ficam no Jira/GitHub nativo.
#   4. Remove as labels crewflow:* do repo (opcional — passa --delete-old)
#
# Uso:
#   ./scripts/migrate-labels.sh owner/repo              # dry-run (só mostra o que faria)
#   ./scripts/migrate-labels.sh owner/repo --apply      # executa a migração
#   ./scripts/migrate-labels.sh owner/repo --apply --delete-old  # migra + remove crewflow:*
#
# Requisitos: gh CLI autenticado com escrita no repo.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -lt 1 ]; then
  echo "uso: $0 owner/repo [--apply] [--delete-old]" >&2
  exit 1
fi

REPO="$1"
APPLY=false
DELETE_OLD=false
for arg in "${@:2}"; do
  case "$arg" in
    --apply)      APPLY=true ;;
    --delete-old) DELETE_OLD=true ;;
    *) echo "argumento desconhecido: $arg" >&2; exit 1 ;;
  esac
done

if [ "$APPLY" = false ]; then
  echo "⚠️  DRY-RUN — nenhuma alteração será feita. Passe --apply para executar."
  echo ""
fi

# ── Mapeamento crewflow → flow ─────────────────────────────────────────────
# formato: "crewflow:X|flow:Y"  (vazio à direita = remover sem substituto)
MAPPING=(
  "crewflow:spec|flow:briefing"
  "crewflow:ready|flow:planning-specs"
  "crewflow:todo|flow:develop-waiting"
  "crewflow:dev|flow:develop-running"
  "crewflow:review|flow:review-waiting"
  "crewflow:review-ok|flow:review-approved"
  "crewflow:review-fail|flow:review-refused"
  "crewflow:qa|flow:qa-waiting"        # qa-testing é manual; qa vai para waiting
  "crewflow:done|flow:done"
  "crewflow:blocked|flow:blocked"
  "crewflow:conflito|flow:merge-conflict"
  "crewflow:merge-conflict|flow:merge-conflict"
  # sem substituto — remover
  "crewflow:reviewed|"
  "crewflow:changes-requested|"
  "crewflow:running|"
  "crewflow:hml-bypass|"
  "crewflow:feature|"
  "crewflow:bug|"
  "crewflow:hotfix|"
  "crewflow:debt|"
  "crewflow:p1|"
  "crewflow:p2|"
  "crewflow:p3|"
)

# ── Etapa 1: criar labels flow:* ───────────────────────────────────────────
echo "=== Etapa 1: criar labels flow:* em $REPO ==="
if [ "$APPLY" = true ]; then
  bash "$SCRIPT_DIR/setup-flow-labels.sh" "$REPO"
else
  echo "  [dry-run] rodaria: setup-flow-labels.sh $REPO"
fi
echo ""

# ── Etapa 2: migrar issues ─────────────────────────────────────────────────
echo "=== Etapa 2: migrar labels nas issues abertas ==="

# buscar todas as issues abertas e filtrar as que têm alguma label crewflow:*
ISSUES=$(gh issue list --repo "$REPO" --state open --limit 500 \
  --json number,labels \
  --jq '.[] | select(.labels[].name | startswith("crewflow:")) | "\(.number)|\([.labels[].name] | join(","))"' \
  2>/dev/null | sort -u || true)

if [ -z "$ISSUES" ]; then
  echo "  nenhuma issue aberta com labels crewflow:* encontrada."
else
  while IFS='|' read -r issue_num current_labels; do
    echo ""
    echo "  issue #$issue_num — labels atuais: $current_labels"
    for entry in "${MAPPING[@]}"; do
      IFS='|' read -r old_label new_label <<< "$entry"
      if echo ",$current_labels," | grep -q ",$old_label,"; then
        if [ -n "$new_label" ]; then
          echo "    $old_label → $new_label"
          if [ "$APPLY" = true ]; then
            gh issue edit "$issue_num" --repo "$REPO" \
              --add-label "$new_label" \
              --remove-label "$old_label" >/dev/null 2>&1 && echo "    ✓" || echo "    FALHOU"
          fi
        else
          echo "    $old_label → (remover, sem substituto)"
          if [ "$APPLY" = true ]; then
            gh issue edit "$issue_num" --repo "$REPO" \
              --remove-label "$old_label" >/dev/null 2>&1 && echo "    ✓" || echo "    FALHOU"
          fi
        fi
      fi
    done
  done <<< "$ISSUES"
fi
echo ""

# ── Etapa 3: remover labels crewflow:* do repo ────────────────────────────
if [ "$DELETE_OLD" = true ]; then
  echo "=== Etapa 3: remover labels crewflow:* do repo ==="
  # listar todas as labels crewflow:* existentes no repo
  CREWFLOW_LABELS=$(gh label list --repo "$REPO" --search "crewflow" --json name --jq '.[].name' 2>/dev/null || true)
  if [ -z "$CREWFLOW_LABELS" ]; then
    echo "  nenhuma label crewflow:* encontrada no repo."
  else
    while IFS= read -r label; do
      echo "  deletando: $label"
      if [ "$APPLY" = true ]; then
        gh label delete "$label" --repo "$REPO" --yes >/dev/null 2>&1 && echo "  ✓" || echo "  FALHOU"
      else
        echo "  [dry-run] deletaria: $label"
      fi
    done <<< "$CREWFLOW_LABELS"
  fi
else
  echo "=== Etapa 3: labels crewflow:* mantidas no repo (passe --delete-old para remover) ==="
fi

echo ""
echo "== done =="
if [ "$APPLY" = false ]; then
  echo ""
  echo "Para executar: $0 $REPO --apply"
  echo "Para migrar + remover crewflow:*: $0 $REPO --apply --delete-old"
fi
