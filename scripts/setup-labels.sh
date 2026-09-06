#!/usr/bin/env bash
# kirocrew-deployment — aplica as 8 labels do fluxo num repo (idempotente via --force).
# Uso: ./scripts/setup-labels.sh owner/repo [owner/repo ...]
set -u

if [ "$#" -lt 1 ]; then
  echo "uso: $0 owner/repo [owner/repo ...]" >&2
  exit 1
fi

# nome|cor(hex sem #)|descricao
LABELS=(
  "ideia/aguardando-spec|FEF3C7|Ideia crua ou em especificacao (so humano)"
  "aguardando-liberacao|FBBF24|Spec revisada, na fila aguardando o humano liberar"
  "aguardando-desenvolvimento|16A34A|Liberado - a esteira pega e implementa"
  "em-desenvolvimento|2563EB|Sessao implementando"
  "em-teste|0EA5E9|Validando (testes/QA) antes de abrir o PR"
  "aguardando-code-review|8B5CF6|PR aberto, esperando revisao humana"
  "acao-necessaria|F97316|Travou, precisa de decisao humana"
  "segurar|DC2626|Nao fazer auto-merge (humano poe antes)"
  "bloqueado|374151|Travado por dependencia"
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
