ALTER TABLE public.execution_plan_reservations
    ADD COLUMN IF NOT EXISTS validated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS validation_snapshot_hash TEXT,
    ADD COLUMN IF NOT EXISTS authority_fingerprint_hash TEXT;

DO $reservation_validation_constraint$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.execution_plan_reservations'::regclass
          AND conname = 'execution_plan_reservation_snapshot_hash_length'
    ) THEN
        ALTER TABLE public.execution_plan_reservations
            ADD CONSTRAINT execution_plan_reservation_snapshot_hash_length
            CHECK (
                validation_snapshot_hash IS NULL
                OR length(validation_snapshot_hash) = 64
            );
    END IF;
END
$reservation_validation_constraint$;

DO $reservation_authority_constraint$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.execution_plan_reservations'::regclass
          AND conname = 'execution_plan_reservation_authority_hash_length'
    ) THEN
        ALTER TABLE public.execution_plan_reservations
            ADD CONSTRAINT execution_plan_reservation_authority_hash_length
            CHECK (
                authority_fingerprint_hash IS NULL
                OR length(authority_fingerprint_hash) = 64
            );
    END IF;
END
$reservation_authority_constraint$;

CREATE INDEX IF NOT EXISTS execution_plan_reservations_validation
    ON public.execution_plan_reservations (validated_at)
    WHERE attempt_id IS NOT NULL;

ALTER TABLE public.picker_order_events
    ADD COLUMN IF NOT EXISTS account_key TEXT,
    ADD COLUMN IF NOT EXISTS symbol TEXT,
    ADD COLUMN IF NOT EXISTS session_date DATE;

ALTER TABLE public.picker_control_state
    ADD COLUMN IF NOT EXISTS prior_close_metric_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS prior_close_observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS prior_close_source TEXT,
    ADD COLUMN IF NOT EXISTS prior_close_artifact_hash TEXT;

DO $prior_close_artifact_constraint$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.picker_control_state'::regclass
          AND conname = 'picker_control_prior_close_artifact_hash_length'
    ) THEN
        ALTER TABLE public.picker_control_state
            ADD CONSTRAINT picker_control_prior_close_artifact_hash_length
            CHECK (
                prior_close_artifact_hash IS NULL
                OR length(prior_close_artifact_hash) = 64
            );
    END IF;
END
$prior_close_artifact_constraint$;

CREATE INDEX IF NOT EXISTS picker_order_events_account_symbol_session
    ON public.picker_order_events (account_key, symbol, session_date, event_type)
    WHERE account_key IS NOT NULL
      AND symbol IS NOT NULL
      AND session_date IS NOT NULL;

COMMENT ON COLUMN public.execution_plan_reservations.validated_at IS
    'Completion time of the broker-state and quote revalidation consumed by a submission claim.';
COMMENT ON COLUMN public.execution_plan_reservations.validation_snapshot_hash IS
    'SHA-256 of the exact fresh broker snapshot used for the reservation revalidation.';
COMMENT ON COLUMN public.execution_plan_reservations.authority_fingerprint_hash IS
    'Signed normalized non-quote broker authority that fresh reservation state must match.';
COMMENT ON COLUMN public.picker_order_events.session_date IS
    'NYSE session identity used to enforce the durable same-session re-entry prohibition.';
COMMENT ON COLUMN public.picker_control_state.prior_close_metric_at IS
    'Timestamp of the official regular-session close valuation metric.';
COMMENT ON COLUMN public.picker_control_state.prior_close_observed_at IS
    'Timestamp when the official-close artifact was observed by the automation.';
COMMENT ON COLUMN public.picker_control_state.prior_close_source IS
    'Code-validated source identifier for the official regular-session close valuation.';
COMMENT ON COLUMN public.picker_control_state.prior_close_artifact_hash IS
    'SHA-256 of the immutable source artifact used for the close-equity anchor.';
