CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS automation_runs (
    run_id TEXT PRIMARY KEY,
    task_name TEXT NOT NULL,
    scheduled_for TIMESTAMPTZ NOT NULL,
    git_sha TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed')),
    lease_token TEXT NOT NULL,
    lease_expires_at TIMESTAMPTZ NOT NULL,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    failure_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (task_name, scheduled_for)
);

CREATE INDEX IF NOT EXISTS automation_runs_active_lease
    ON automation_runs (lease_expires_at)
    WHERE status = 'running';

CREATE TABLE IF NOT EXISTS execution_plans (
    plan_id TEXT PRIMARY KEY,
    draft_hash TEXT NOT NULL UNIQUE CHECK (length(draft_hash) = 64),
    run_id TEXT NOT NULL REFERENCES automation_runs(run_id),
    account_key TEXT NOT NULL,
    trade_date DATE NOT NULL,
    research_batch_id TEXT NOT NULL DEFAULT '',
    snapshot_hash TEXT NOT NULL CHECK (length(snapshot_hash) = 64),
    planned_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft', 'reviewed', 'awaiting_confirmation', 'confirmed',
            'reserved', 'submitting', 'submitted', 'unknown',
            'partially_filled', 'filled', 'cancelled', 'rejected',
            'failed', 'expired', 'invalidated', 'reconciled'
        )),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at > planned_at),
    CHECK (expires_at - planned_at <= interval '5 minutes')
);

CREATE INDEX IF NOT EXISTS execution_plans_account_date
    ON execution_plans (account_key, trade_date, planned_at DESC);

CREATE TABLE IF NOT EXISTS execution_plan_reviews (
    plan_id TEXT PRIMARY KEY REFERENCES execution_plans(plan_id),
    draft_hash TEXT NOT NULL CHECK (length(draft_hash) = 64),
    review_hash TEXT NOT NULL UNIQUE CHECK (length(review_hash) = 64),
    review_payload JSONB NOT NULL,
    reviewed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS execution_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE REFERENCES execution_plans(plan_id),
    review_hash TEXT NOT NULL CHECK (length(review_hash) = 64),
    actor_ref TEXT NOT NULL,
    confirmed_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (expires_at > confirmed_at)
);

CREATE TABLE IF NOT EXISTS execution_order_attempts (
    attempt_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES execution_plans(plan_id),
    confirmation_id TEXT NOT NULL REFERENCES execution_confirmations(confirmation_id),
    account_key TEXT NOT NULL,
    ref_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    broker_request JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'prepared'
        CHECK (state IN (
            'prepared', 'reserved', 'submitting', 'submitted', 'unknown',
            'partially_filled', 'filled', 'cancelled', 'rejected',
            'failed', 'expired', 'invalidated', 'reconciled'
        )),
    broker_order_id TEXT,
    latest_response JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS execution_order_attempts_nonterminal
    ON execution_order_attempts (account_key, updated_at)
    WHERE state IN (
        'prepared', 'reserved', 'submitting', 'submitted', 'unknown', 'partially_filled'
    );

CREATE TABLE IF NOT EXISTS execution_attempt_transitions (
    transition_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL REFERENCES execution_order_attempts(attempt_id),
    from_state TEXT,
    to_state TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64)
);

CREATE INDEX IF NOT EXISTS execution_attempt_transitions_attempt
    ON execution_attempt_transitions (attempt_id, occurred_at);

CREATE TABLE IF NOT EXISTS execution_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES execution_plans(plan_id),
    result_hash TEXT NOT NULL UNIQUE CHECK (length(result_hash) = 64),
    clean BOOLEAN NOT NULL,
    payload JSONB NOT NULL,
    reconciled_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS execution_reconciliations_plan
    ON execution_reconciliations (plan_id, reconciled_at DESC);

CREATE TABLE IF NOT EXISTS execution_audit_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    run_id TEXT REFERENCES automation_runs(run_id),
    plan_id TEXT REFERENCES execution_plans(plan_id),
    attempt_id TEXT REFERENCES execution_order_attempts(attempt_id),
    ref_id TEXT,
    payload JSONB NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64)
);

CREATE INDEX IF NOT EXISTS execution_audit_events_timeline
    ON execution_audit_events (occurred_at, event_id);

CREATE TABLE IF NOT EXISTS cloud_runtime_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES automation_runs(run_id),
    artifact_type TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
    payload JSONB NOT NULL,
    source_uri TEXT,
    observed_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, artifact_type, content_hash)
);

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    node_id TEXT PRIMARY KEY,
    node_type TEXT NOT NULL,
    title TEXT NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
    edge_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id),
    target_id TEXT NOT NULL REFERENCES knowledge_nodes(node_id),
    relation TEXT NOT NULL,
    sign TEXT NOT NULL,
    horizon TEXT NOT NULL,
    causality TEXT NOT NULL CHECK (causality IN ('hypothesis', 'non_causal')),
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_id, target_id, relation, horizon)
);

CREATE TABLE IF NOT EXISTS knowledge_observations (
    observation_id TEXT PRIMARY KEY,
    edge_id TEXT NOT NULL REFERENCES knowledge_edges(edge_id),
    run_id TEXT REFERENCES automation_runs(run_id),
    prediction_id TEXT,
    outcome_id TEXT,
    evidence_id TEXT,
    document_hash TEXT,
    decision_date DATE NOT NULL,
    horizon TEXT NOT NULL,
    regime TEXT,
    polarity TEXT NOT NULL CHECK (polarity IN ('supports', 'contradicts', 'neutral')),
    measured_result DOUBLE PRECISION,
    observed_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    observation_hash TEXT NOT NULL UNIQUE CHECK (length(observation_hash) = 64)
);

ALTER TABLE execution_plan_reservations
    ADD COLUMN IF NOT EXISTS plan_id TEXT REFERENCES execution_plans(plan_id),
    ADD COLUMN IF NOT EXISTS confirmation_id TEXT REFERENCES execution_confirmations(confirmation_id),
    ADD COLUMN IF NOT EXISTS attempt_id TEXT REFERENCES execution_order_attempts(attempt_id);

ALTER TABLE picker_control_state
    ADD COLUMN IF NOT EXISTS halt_scope TEXT NOT NULL DEFAULT 'entries';
