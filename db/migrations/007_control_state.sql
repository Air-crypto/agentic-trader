ALTER TABLE picker_control_state
    ADD COLUMN IF NOT EXISTS prior_close_date DATE;

COMMENT ON COLUMN picker_control_state.prior_close_equity IS
    'Immutable official-close equity anchor for prior_close_date; never intraday equity.';

CREATE UNIQUE INDEX IF NOT EXISTS picker_order_events_ref_event
    ON picker_order_events (ref_id, event_type)
    WHERE ref_id IS NOT NULL;
