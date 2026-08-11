CREATE TABLE IF NOT EXISTS picker_research_cycles (
    cycle_id TEXT PRIMARY KEY,
    as_of DATE NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'pending', 'finalized', 'failed')),
    batch_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS picker_research_cycles_status_date
    ON picker_research_cycles (as_of, status, started_at DESC);
