-- depends:

CREATE TABLE workflow_runs (
    id           TEXT PRIMARY KEY,   -- uuid gerado no trigger
    workflow_id  TEXT NOT NULL,      -- referência ao id: do YAML
    status       TEXT NOT NULL       -- pending | running | completed | failed
                     CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    current_node TEXT,               -- nó em execução agora
    session_key  TEXT,               -- session_key do nó open_session ativo (dashboard_chat-N-ts)
    context_json TEXT,               -- JSON com o contexto acumulado do run ($nodes.*.output.*)
    started_at   TEXT NOT NULL,      -- ISO 8601
    updated_at   TEXT NOT NULL,
    finished_at  TEXT                -- NULL enquanto em execução
);

CREATE INDEX idx_workflow_runs_status ON workflow_runs (status);
CREATE INDEX idx_workflow_runs_workflow_id ON workflow_runs (workflow_id);
