-- depends: 0002.create-workflow-node-states

-- Diferencia o motivo de encerramento não-nominal de um nó:
--   failed     -> agente/script declarou falha explicitamente (WORKFLOW_EXIT exit_status=failed / exit_code != 0)
--   timed_out  -> sessão encerrou sem escrever WORKFLOW_EXIT dentro do node_ttl_secs
--   error      -> exceção não tratada no próprio tick do engine
--   NULL       -> nó concluído com sucesso (status = completed)
ALTER TABLE workflow_node_states ADD COLUMN error_kind TEXT
    CHECK (error_kind IS NULL OR error_kind IN ('failed', 'timed_out', 'error'));

-- Adiciona node_ttl_secs em workflow_runs para saber o prazo de cada run
-- (herdado do campo node_ttl_secs do workflow YAML; default 3600s = 1h)
ALTER TABLE workflow_runs ADD COLUMN node_ttl_secs INTEGER NOT NULL DEFAULT 3600;
