-- depends: 0003.add-error-kind-and-ttl

-- Tokens de gate humano: cada nó action/gate gera um token que a UI
-- apresenta como botões "Aprovar" / "Rejeitar". O engine polling verifica
-- se o token foi respondido e avança o run conforme a decisão.
CREATE TABLE workflow_gate_tokens (
    token        TEXT PRIMARY KEY,           -- uuid gerado ao entrar no nó gate
    run_id       TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    node_id      TEXT NOT NULL,
    prompt       TEXT NOT NULL,              -- pergunta exibida na UI
    options_json TEXT NOT NULL DEFAULT '["approve","reject"]',  -- opções disponíveis
    decision     TEXT,                       -- NULL = pendente; valor = opção escolhida
    decided_at   TEXT,                       -- ISO 8601, NULL enquanto pendente
    expires_at   TEXT NOT NULL,              -- ISO 8601; engine trata como timed_out após esse ts
    created_at   TEXT NOT NULL
);

CREATE INDEX idx_gate_tokens_run_node ON workflow_gate_tokens (run_id, node_id);
CREATE INDEX idx_gate_tokens_decision ON workflow_gate_tokens (decision) WHERE decision IS NULL;
