-- Every application table is private to the direct PostgreSQL runtime role.
-- Migration 010 covered the cloud-runtime tables; this closes the older picker,
-- option, learning, control, and execution-budget tables as well. RLS is not
-- forced because DATABASE_URL intentionally uses the table-owner connection.

ALTER TABLE public.picker_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.evidence_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.picker_drafts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.critic_verdicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.decision_packets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.active_theses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.picker_order_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.picker_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.picker_control_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.option_decision_packets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.active_option_positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.option_resource_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.option_share_encumbrances ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.option_order_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_daily_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.picker_research_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.picker_pending_research_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.picker_research_cycles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.learning_prediction_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.learning_predictions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.learning_forward_outcomes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.learning_evaluation_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.learning_promotion_events ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE
    public.picker_runs,
    public.evidence_versions,
    public.picker_drafts,
    public.critic_verdicts,
    public.decision_packets,
    public.active_theses,
    public.picker_order_events,
    public.picker_outcomes,
    public.picker_control_state,
    public.option_decision_packets,
    public.active_option_positions,
    public.option_resource_reservations,
    public.option_share_encumbrances,
    public.option_order_events,
    public.execution_daily_usage,
    public.picker_research_batches,
    public.picker_pending_research_batches,
    public.picker_research_cycles,
    public.learning_prediction_batches,
    public.learning_predictions,
    public.learning_forward_outcomes,
    public.learning_evaluation_reports,
    public.learning_promotion_events
FROM PUBLIC;

DO $revoke_legacy_api_roles$
DECLARE
    api_role TEXT;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON TABLE '
                'public.picker_runs, public.evidence_versions, '
                'public.picker_drafts, public.critic_verdicts, '
                'public.decision_packets, public.active_theses, '
                'public.picker_order_events, public.picker_outcomes, '
                'public.picker_control_state, public.option_decision_packets, '
                'public.active_option_positions, public.option_resource_reservations, '
                'public.option_share_encumbrances, public.option_order_events, '
                'public.execution_daily_usage, public.picker_research_batches, '
                'public.picker_pending_research_batches, public.picker_research_cycles, '
                'public.learning_prediction_batches, public.learning_predictions, '
                'public.learning_forward_outcomes, public.learning_evaluation_reports, '
                'public.learning_promotion_events FROM %I',
                api_role
            );
        END IF;
    END LOOP;
END
$revoke_legacy_api_roles$;

COMMENT ON TABLE public.picker_runs IS 'Private agentic-trader application data.';
COMMENT ON TABLE public.evidence_versions IS 'Private agentic-trader evidence ledger.';
COMMENT ON TABLE public.picker_control_state IS 'Private durable trading controls.';
COMMENT ON TABLE public.execution_daily_usage IS 'Private durable execution budget.';
COMMENT ON TABLE public.learning_predictions IS 'Private learning telemetry.';
