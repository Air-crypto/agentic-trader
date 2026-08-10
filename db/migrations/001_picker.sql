CREATE TABLE IF NOT EXISTS picker_runs (
    run_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    as_of DATE NOT NULL,
    model_id TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS picker_control_state (
    account_key TEXT PRIMARY KEY,
    halted BOOLEAN NOT NULL DEFAULT FALSE,
    halt_reason TEXT,
    high_water_mark DOUBLE PRECISION,
    prior_close_equity DOUBLE PRECISION,
    cooldown_until DATE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidence_versions (
    evidence_id TEXT NOT NULL,
    document_hash TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    retrieved_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (evidence_id, document_hash)
);

CREATE INDEX IF NOT EXISTS evidence_versions_knowledge_time
    ON evidence_versions (first_seen_at, published_at);

CREATE TABLE IF NOT EXISTS picker_drafts (
    draft_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES picker_runs(run_id),
    symbol TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS critic_verdicts (
    draft_id TEXT PRIMARY KEY REFERENCES picker_drafts(draft_id),
    created_at TIMESTAMPTZ NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'veto')),
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS picker_research_batches (
    batch_id TEXT PRIMARY KEY,
    as_of DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    prompt_hash TEXT NOT NULL,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'staged'
        CHECK (status IN ('staged', 'authorized', 'rejected', 'consumed')),
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS picker_research_batches_status_date
    ON picker_research_batches (status, as_of, created_at DESC);

CREATE TABLE IF NOT EXISTS decision_packets (
    packet_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES picker_runs(run_id),
    draft_id TEXT NOT NULL REFERENCES picker_drafts(draft_id),
    symbol TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('buy', 'close')),
    valid_for_date DATE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    packet_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'authorized'
        CHECK (status IN ('authorized', 'revoked', 'consumed')),
    payload JSONB NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_live_packet_per_symbol_action_day
    ON decision_packets (valid_for_date, symbol, action)
    WHERE status = 'authorized';

CREATE TABLE IF NOT EXISTS active_theses (
    pick_id TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL REFERENCES decision_packets(packet_id),
    symbol TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_date DATE NOT NULL,
    expiry_date DATE NOT NULL,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS active_theses_status_symbol
    ON active_theses (status, symbol);

CREATE TABLE IF NOT EXISTS picker_order_events (
    event_id BIGSERIAL PRIMARY KEY,
    pick_id TEXT,
    packet_id TEXT,
    ref_id TEXT,
    broker_order_id TEXT,
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS picker_outcomes (
    packet_id TEXT NOT NULL REFERENCES decision_packets(packet_id),
    horizon_days INTEGER NOT NULL CHECK (horizon_days IN (1, 3, 5, 20, 60)),
    measured_at TIMESTAMPTZ NOT NULL,
    raw_return DOUBLE PRECISION,
    spy_abnormal_return DOUBLE PRECISION,
    sector_abnormal_return DOUBLE PRECISION,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (packet_id, horizon_days)
);
