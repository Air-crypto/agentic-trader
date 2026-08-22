-- Immutable, point-in-time predictions and forward outcomes.
-- Insert a batch and all four arm rows for every candidate in one transaction;
-- the deferred completeness trigger validates the full universe at commit.

CREATE TABLE IF NOT EXISTS learning_prediction_batches (
    batch_id TEXT PRIMARY KEY,
    decision_date DATE NOT NULL,
    decision_at TIMESTAMPTZ NOT NULL,
    frozen_at TIMESTAMPTZ NOT NULL,
    expected_candidate_count INTEGER NOT NULL CHECK (expected_candidate_count > 0),
    batch_hash TEXT NOT NULL UNIQUE CHECK (batch_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (frozen_at <= decision_at),
    CHECK (decision_date = (decision_at AT TIME ZONE 'UTC')::date)
);

CREATE TABLE IF NOT EXISTS learning_predictions (
    prediction_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    symbol TEXT NOT NULL CHECK (symbol <> ''),
    sector_benchmark TEXT NOT NULL CHECK (sector_benchmark <> ''),
    arm TEXT NOT NULL CHECK (
        arm IN ('factor_only', 'llm_only', 'hybrid', 'do_nothing')
    ),
    action TEXT NOT NULL CHECK (action IN ('buy', 'reject', 'hold')),
    selected BOOLEAN NOT NULL,
    score DOUBLE PRECISION NOT NULL CHECK (
        score NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    position_weight DOUBLE PRECISION NOT NULL CHECK (
        position_weight BETWEEN -1 AND 1
        AND position_weight NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    expected_turnover DOUBLE PRECISION NOT NULL CHECK (
        expected_turnover BETWEEN 0 AND 2
        AND expected_turnover NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    decision_date DATE NOT NULL,
    decision_at TIMESTAMPTZ NOT NULL,
    frozen_at TIMESTAMPTZ NOT NULL,
    data_cutoff_at TIMESTAMPTZ NOT NULL,
    model_id TEXT NOT NULL CHECK (model_id <> ''),
    model_hash TEXT NOT NULL CHECK (model_hash ~ '^[0-9a-f]{64}$'),
    prompt_hash TEXT NOT NULL CHECK (prompt_hash ~ '^[0-9a-f]{64}$'),
    feature_hash TEXT NOT NULL CHECK (feature_hash ~ '^[0-9a-f]{64}$'),
    data_snapshot_hash TEXT NOT NULL CHECK (data_snapshot_hash ~ '^[0-9a-f]{64}$'),
    entry_price DOUBLE PRECISION NOT NULL CHECK (
        entry_price > 0
        AND entry_price NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    entry_spy_price DOUBLE PRECISION NOT NULL CHECK (
        entry_spy_price > 0
        AND entry_spy_price NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    entry_sector_price DOUBLE PRECISION NOT NULL CHECK (
        entry_sector_price > 0
        AND entry_sector_price NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    prediction_hash TEXT NOT NULL UNIQUE CHECK (prediction_hash ~ '^[0-9a-f]{64}$'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (batch_id, candidate_id, arm),
    FOREIGN KEY (batch_id)
        REFERENCES learning_prediction_batches(batch_id)
        DEFERRABLE INITIALLY IMMEDIATE,
    CHECK (data_cutoff_at <= frozen_at AND frozen_at <= decision_at),
    CHECK (selected = (abs(position_weight) > 1e-12)),
    CHECK (selected OR expected_turnover = 0),
    CHECK (action NOT IN ('reject', 'hold') OR NOT selected),
    CHECK (action <> 'buy' OR position_weight > 0),
    CHECK (
        arm <> 'do_nothing'
        OR (action = 'hold' AND NOT selected AND expected_turnover = 0)
    )
);

CREATE INDEX IF NOT EXISTS learning_predictions_decision_arm
    ON learning_predictions (decision_date, arm, candidate_id);

CREATE TABLE IF NOT EXISTS learning_forward_outcomes (
    outcome_id TEXT PRIMARY KEY,
    prediction_id TEXT NOT NULL REFERENCES learning_predictions(prediction_id),
    prediction_hash TEXT NOT NULL CHECK (prediction_hash ~ '^[0-9a-f]{64}$'),
    batch_hash TEXT NOT NULL CHECK (batch_hash ~ '^[0-9a-f]{64}$'),
    candidate_id TEXT NOT NULL,
    arm TEXT NOT NULL CHECK (
        arm IN ('factor_only', 'llm_only', 'hybrid', 'do_nothing')
    ),
    decision_date DATE NOT NULL,
    horizon_sessions INTEGER NOT NULL CHECK (horizon_sessions IN (1, 3, 5, 20, 60)),
    mark_session_date DATE NOT NULL,
    mark_observed_at TIMESTAMPTZ NOT NULL,
    mark_available_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    gross_return DOUBLE PRECISION NOT NULL CHECK (
        gross_return >= -1
        AND gross_return NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    spy_relative_return DOUBLE PRECISION NOT NULL CHECK (
        spy_relative_return NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    sector_relative_return DOUBLE PRECISION NOT NULL CHECK (
        sector_relative_return NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    strategy_gross_return DOUBLE PRECISION NOT NULL CHECK (
        strategy_gross_return NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    turnover DOUBLE PRECISION NOT NULL CHECK (
        turnover >= 0
        AND turnover NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    cost_bps DOUBLE PRECISION NOT NULL CHECK (
        cost_bps >= 0
        AND cost_bps NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    cost_return DOUBLE PRECISION NOT NULL CHECK (
        cost_return >= 0
        AND cost_return NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    strategy_net_return DOUBLE PRECISION NOT NULL CHECK (
        strategy_net_return NOT IN (
            'NaN'::DOUBLE PRECISION,
            'Infinity'::DOUBLE PRECISION,
            '-Infinity'::DOUBLE PRECISION
        )
    ),
    outcome_hash TEXT NOT NULL UNIQUE CHECK (outcome_hash ~ '^[0-9a-f]{64}$'),
    UNIQUE (prediction_id, horizon_sessions),
    CHECK (decision_date < mark_session_date),
    CHECK (mark_observed_at <= mark_available_at),
    CHECK (mark_available_at <= recorded_at),
    CHECK (mark_session_date = (mark_observed_at AT TIME ZONE 'UTC')::date),
    CHECK (abs(cost_return - turnover * cost_bps / 10000.0) <= 1e-12),
    CHECK (abs(strategy_net_return - (strategy_gross_return - cost_return)) <= 1e-12)
);

CREATE INDEX IF NOT EXISTS learning_outcomes_horizon_date
    ON learning_forward_outcomes (horizon_sessions, decision_date, arm);

CREATE TABLE IF NOT EXISTS learning_evaluation_reports (
    report_id TEXT PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL,
    as_of TIMESTAMPTZ NOT NULL,
    horizon_sessions INTEGER NOT NULL CHECK (horizon_sessions IN (1, 3, 5, 20, 60)),
    policy_hash TEXT NOT NULL CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
    report_hash TEXT NOT NULL UNIQUE CHECK (report_hash ~ '^[0-9a-f]{64}$'),
    passed BOOLEAN NOT NULL,
    payload JSONB NOT NULL,
    CHECK (generated_at <= as_of)
);

CREATE TABLE IF NOT EXISTS learning_promotion_events (
    event_id TEXT PRIMARY KEY,
    system_key TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    current_state TEXT NOT NULL CHECK (current_state IN ('shadow', 'canary', 'live')),
    requested_state TEXT NOT NULL CHECK (requested_state IN ('shadow', 'canary', 'live')),
    resulting_state TEXT NOT NULL CHECK (resulting_state IN ('shadow', 'canary', 'live')),
    approved BOOLEAN NOT NULL,
    report_hash TEXT NOT NULL REFERENCES learning_evaluation_reports(report_hash),
    reasons JSONB NOT NULL DEFAULT '[]'::JSONB,
    UNIQUE (system_key, occurred_at, event_id),
    CHECK (
        (approved AND resulting_state = requested_state)
        OR (NOT approved AND resulting_state = current_state)
    )
);

CREATE OR REPLACE FUNCTION reject_learning_row_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
END;
$$;

DO $$
DECLARE
    table_name TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'learning_prediction_batches',
        'learning_predictions',
        'learning_forward_outcomes',
        'learning_evaluation_reports',
        'learning_promotion_events'
    ]
    LOOP
        trigger_name := table_name || '_append_only';
        IF NOT EXISTS (
            SELECT 1
            FROM pg_trigger
            WHERE tgname = trigger_name
              AND tgrelid = table_name::regclass
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
                'FOR EACH ROW EXECUTE FUNCTION reject_learning_row_mutation()',
                trigger_name,
                table_name
            );
        END IF;
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION validate_learning_prediction_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    parent learning_prediction_batches%ROWTYPE;
BEGIN
    SELECT * INTO parent
    FROM learning_prediction_batches
    WHERE batch_id = NEW.batch_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown prediction batch %', NEW.batch_id;
    END IF;
    IF NEW.decision_date <> parent.decision_date
        OR NEW.decision_at <> parent.decision_at
        OR NEW.frozen_at > parent.frozen_at
    THEN
        RAISE EXCEPTION 'Prediction does not match batch point-in-time identity';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'learning_prediction_matches_batch'
          AND tgrelid = 'learning_predictions'::regclass
    ) THEN
        CREATE TRIGGER learning_prediction_matches_batch
        BEFORE INSERT ON learning_predictions
        FOR EACH ROW EXECUTE FUNCTION validate_learning_prediction_insert();
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION validate_complete_learning_batch()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    total_rows INTEGER;
    candidate_rows INTEGER;
    factor_rows INTEGER;
    llm_rows INTEGER;
    hybrid_rows INTEGER;
    nothing_rows INTEGER;
    identity_conflicts INTEGER;
    max_exposure DOUBLE PRECISION;
    target_batch_id TEXT;
    expected_count INTEGER;
BEGIN
    target_batch_id := NEW.batch_id;
    SELECT expected_candidate_count INTO expected_count
    FROM learning_prediction_batches
    WHERE batch_id = target_batch_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown learning batch %', target_batch_id;
    END IF;
    SELECT
        count(*),
        count(DISTINCT candidate_id),
        count(*) FILTER (WHERE arm = 'factor_only'),
        count(*) FILTER (WHERE arm = 'llm_only'),
        count(*) FILTER (WHERE arm = 'hybrid'),
        count(*) FILTER (WHERE arm = 'do_nothing')
    INTO total_rows, candidate_rows, factor_rows, llm_rows, hybrid_rows, nothing_rows
    FROM learning_predictions
    WHERE batch_id = target_batch_id;

    IF total_rows <> expected_count * 4
        OR candidate_rows <> expected_count
        OR factor_rows <> expected_count
        OR llm_rows <> expected_count
        OR hybrid_rows <> expected_count
        OR nothing_rows <> expected_count
    THEN
        RAISE EXCEPTION 'Learning batch % omits candidates or experiment arms', target_batch_id;
    END IF;

    SELECT count(*) INTO identity_conflicts
    FROM (
        SELECT candidate_id
        FROM learning_predictions
        WHERE batch_id = target_batch_id
        GROUP BY candidate_id
        HAVING count(DISTINCT symbol) > 1
            OR count(DISTINCT sector_benchmark) > 1
            OR count(DISTINCT entry_price) > 1
            OR count(DISTINCT entry_spy_price) > 1
            OR count(DISTINCT entry_sector_price) > 1
    ) AS inconsistent_candidates;
    IF identity_conflicts > 0 THEN
        RAISE EXCEPTION 'Learning batch % has inconsistent candidate identities', target_batch_id;
    END IF;

    SELECT max(exposure) INTO max_exposure
    FROM (
        SELECT arm, sum(abs(position_weight)) AS exposure
        FROM learning_predictions
        WHERE batch_id = target_batch_id
        GROUP BY arm
    ) AS arm_exposure;
    IF max_exposure > 1.0 + 1e-12 THEN
        RAISE EXCEPTION 'Learning batch % exceeds 100%% gross exposure', target_batch_id;
    END IF;
    RETURN NULL;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'learning_batch_complete_at_commit'
          AND tgrelid = 'learning_prediction_batches'::regclass
    ) THEN
        CREATE CONSTRAINT TRIGGER learning_batch_complete_at_commit
        AFTER INSERT ON learning_prediction_batches
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_complete_learning_batch();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'learning_prediction_preserves_complete_batch'
          AND tgrelid = 'learning_predictions'::regclass
    ) THEN
        CREATE CONSTRAINT TRIGGER learning_prediction_preserves_complete_batch
        AFTER INSERT ON learning_predictions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION validate_complete_learning_batch();
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION validate_learning_outcome_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    prediction learning_predictions%ROWTYPE;
    expected_batch_hash TEXT;
    existing_mark learning_forward_outcomes%ROWTYPE;
BEGIN
    SELECT * INTO prediction
    FROM learning_predictions
    WHERE prediction_id = NEW.prediction_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Unknown learning prediction %', NEW.prediction_id;
    END IF;
    SELECT batch_hash INTO expected_batch_hash
    FROM learning_prediction_batches
    WHERE batch_id = prediction.batch_id;
    IF NEW.prediction_hash <> prediction.prediction_hash
        OR NEW.batch_hash <> expected_batch_hash
        OR NEW.candidate_id <> prediction.candidate_id
        OR NEW.arm <> prediction.arm
        OR NEW.decision_date <> prediction.decision_date
        OR NEW.mark_observed_at <= prediction.decision_at
        OR prediction.recorded_at > NEW.mark_available_at
        OR abs(
            NEW.strategy_gross_return - prediction.position_weight * NEW.gross_return
        ) > 1e-12
        OR abs(
            NEW.turnover
            - CASE WHEN prediction.selected THEN prediction.expected_turnover ELSE 0 END
        ) > 1e-12
    THEN
        RAISE EXCEPTION 'Outcome does not bind to its frozen prediction';
    END IF;
    IF NEW.recorded_at > now() + INTERVAL '5 minutes' THEN
        RAISE EXCEPTION 'Future-dated learning outcomes are prohibited';
    END IF;
    SELECT * INTO existing_mark
    FROM learning_forward_outcomes
    WHERE batch_hash = NEW.batch_hash
      AND candidate_id = NEW.candidate_id
      AND horizon_sessions = NEW.horizon_sessions
    LIMIT 1;
    IF FOUND AND (
        NEW.mark_session_date <> existing_mark.mark_session_date
        OR NEW.mark_observed_at <> existing_mark.mark_observed_at
        OR NEW.mark_available_at <> existing_mark.mark_available_at
        OR NEW.gross_return <> existing_mark.gross_return
        OR NEW.spy_relative_return <> existing_mark.spy_relative_return
        OR NEW.sector_relative_return <> existing_mark.sector_relative_return
    ) THEN
        RAISE EXCEPTION 'Experiment arms must share one candidate market outcome';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'learning_outcome_matches_prediction'
          AND tgrelid = 'learning_forward_outcomes'::regclass
    ) THEN
        CREATE TRIGGER learning_outcome_matches_prediction
        BEFORE INSERT ON learning_forward_outcomes
        FOR EACH ROW EXECUTE FUNCTION validate_learning_outcome_insert();
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION validate_learning_promotion_insert()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    latest_state TEXT;
    report_passed BOOLEAN;
    current_rank INTEGER;
    requested_rank INTEGER;
BEGIN
    SELECT resulting_state INTO latest_state
    FROM learning_promotion_events
    WHERE system_key = NEW.system_key
    ORDER BY occurred_at DESC, event_id DESC
    LIMIT 1;
    IF latest_state IS NULL THEN
        latest_state := 'shadow';
    END IF;
    IF NEW.current_state <> latest_state THEN
        RAISE EXCEPTION 'Promotion current state is stale';
    END IF;
    SELECT passed INTO report_passed
    FROM learning_evaluation_reports
    WHERE report_hash = NEW.report_hash;
    current_rank := array_position(ARRAY['shadow', 'canary', 'live'], NEW.current_state);
    requested_rank := array_position(ARRAY['shadow', 'canary', 'live'], NEW.requested_state);
    IF NEW.approved AND requested_rank > current_rank THEN
        IF requested_rank <> current_rank + 1 OR report_passed IS NOT TRUE THEN
            RAISE EXCEPTION 'Promotion requires a passing report and one-state advance';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'learning_promotion_fails_closed'
          AND tgrelid = 'learning_promotion_events'::regclass
    ) THEN
        CREATE TRIGGER learning_promotion_fails_closed
        BEFORE INSERT ON learning_promotion_events
        FOR EACH ROW EXECUTE FUNCTION validate_learning_promotion_insert();
    END IF;
END;
$$;

CREATE OR REPLACE VIEW learning_current_state AS
SELECT DISTINCT ON (system_key)
    system_key,
    resulting_state,
    occurred_at,
    report_hash
FROM learning_promotion_events
ORDER BY system_key, occurred_at DESC, event_id DESC;
