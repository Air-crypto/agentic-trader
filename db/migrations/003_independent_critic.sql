CREATE TABLE IF NOT EXISTS picker_pending_research_batches (
    batch_id TEXT PRIMARY KEY,
    as_of DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    prompt_hash TEXT NOT NULL CHECK (length(prompt_hash) = 64),
    analyst_model_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'finalized', 'rejected')),
    payload JSONB NOT NULL,
    finalized_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS picker_pending_research_batches_status_date
    ON picker_pending_research_batches (status, as_of, created_at DESC);

CREATE OR REPLACE FUNCTION reject_pending_research_content_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.batch_id IS DISTINCT FROM OLD.batch_id
        OR NEW.as_of IS DISTINCT FROM OLD.as_of
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
        OR NEW.prompt_hash IS DISTINCT FROM OLD.prompt_hash
        OR NEW.analyst_model_id IS DISTINCT FROM OLD.analyst_model_id
        OR NEW.payload IS DISTINCT FROM OLD.payload
    THEN
        RAISE EXCEPTION 'pending research batch content is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'pending_research_content_is_immutable'
          AND tgrelid = 'picker_pending_research_batches'::regclass
    ) THEN
        CREATE TRIGGER pending_research_content_is_immutable
        BEFORE UPDATE ON picker_pending_research_batches
        FOR EACH ROW
        EXECUTE FUNCTION reject_pending_research_content_update();
    END IF;
END;
$$;
