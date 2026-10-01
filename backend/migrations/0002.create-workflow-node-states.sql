-- depends: 0001.create-workflow-runs

CREATE TABLE workflow_node_states (
    run_id       TEXT NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    node_id      TEXT NOT NULL,      -- id do nó no YAML
    status       TEXT NOT NULL       -- pending | running | completed | failed | skipped
                     CHECK (status IN ('pending', 'running', 'completed', 'failed', 'skipped')),
    output_json  TEXT,               -- JSON com o output capturado do nó
    exit_status  TEXT,               -- exit_status semântico (pr_opened, approved, blocked...)
    exit_code    INTEGER,            -- código de saída do script (0 = ok); NULL para open_session
    started_at   TEXT,
    finished_at  TEXT,               -- NULL enquanto em execução
    PRIMARY KEY (run_id, node_id)
);

CREATE INDEX idx_workflow_node_states_run_id ON workflow_node_states (run_id);
