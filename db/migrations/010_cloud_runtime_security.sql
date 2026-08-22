-- Cloud runtime state contains account-linked decisions, broker requests, and
-- learned relationships. It is intentionally private to the direct PostgreSQL
-- runtime connection. RLS is enabled but not forced so that the table owner
-- used by DATABASE_URL retains access without adding a public API policy.

ALTER TABLE public.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.automation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_plan_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_confirmations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_order_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_attempt_transitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_reconciliations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.cloud_runtime_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.knowledge_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.knowledge_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.knowledge_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.execution_plan_reservations ENABLE ROW LEVEL SECURITY;

-- PUBLIC has no legitimate table path. Supabase's API roles may not exist in
-- non-Supabase development PostgreSQL instances, so revoke from them only when
-- present. service_role bypasses RLS in Supabase and therefore must also lose
-- table privileges. No policies are created: PostgREST access remains closed.
REVOKE ALL PRIVILEGES ON TABLE
    public.schema_migrations,
    public.automation_runs,
    public.execution_plans,
    public.execution_plan_reviews,
    public.execution_confirmations,
    public.execution_order_attempts,
    public.execution_attempt_transitions,
    public.execution_reconciliations,
    public.execution_audit_events,
    public.cloud_runtime_artifacts,
    public.knowledge_nodes,
    public.knowledge_edges,
    public.knowledge_observations,
    public.execution_plan_reservations
FROM PUBLIC;

DO $revoke_api_roles$
DECLARE
    api_role TEXT;
BEGIN
    FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
            EXECUTE format(
                'REVOKE ALL PRIVILEGES ON TABLE '
                'public.schema_migrations, '
                'public.automation_runs, '
                'public.execution_plans, '
                'public.execution_plan_reviews, '
                'public.execution_confirmations, '
                'public.execution_order_attempts, '
                'public.execution_attempt_transitions, '
                'public.execution_reconciliations, '
                'public.execution_audit_events, '
                'public.cloud_runtime_artifacts, '
                'public.knowledge_nodes, '
                'public.knowledge_edges, '
                'public.knowledge_observations, '
                'public.execution_plan_reservations FROM %I',
                api_role
            );
        END IF;
    END LOOP;
END
$revoke_api_roles$;

COMMENT ON TABLE public.schema_migrations IS
    'Private deployment ledger; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.automation_runs IS
    'Private durable automation leases; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.execution_plans IS
    'Private immutable live-trade plans; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.execution_plan_reviews IS
    'Private broker review records; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.execution_confirmations IS
    'Private exact-plan confirmations; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.execution_order_attempts IS
    'Private broker order-attempt state; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.execution_attempt_transitions IS
    'Private immutable attempt transitions; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.execution_reconciliations IS
    'Private broker reconciliation results; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.execution_audit_events IS
    'Private execution audit trail; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.cloud_runtime_artifacts IS
    'Private durable research artifacts; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.knowledge_nodes IS
    'Private runtime knowledge-graph nodes; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.knowledge_edges IS
    'Private runtime knowledge-graph edges; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.knowledge_observations IS
    'Private runtime knowledge-graph observations; direct runtime PostgreSQL role only, never API-role access.';
COMMENT ON TABLE public.execution_plan_reservations IS
    'Private execution budget reservations; cloud links must identify one consistent plan, confirmation, and attempt.';

-- The leading primary-key columns already make these tuples unique. Explicit
-- composite indexes let PostgreSQL use them as foreign-key targets and make the
-- intended cross-table identity relationship auditable.
CREATE UNIQUE INDEX IF NOT EXISTS execution_confirmations_id_plan_key
    ON public.execution_confirmations (confirmation_id, plan_id);

CREATE UNIQUE INDEX IF NOT EXISTS execution_order_attempts_id_plan_confirmation_key
    ON public.execution_order_attempts (attempt_id, plan_id, confirmation_id);

CREATE INDEX IF NOT EXISTS execution_plan_reservations_plan
    ON public.execution_plan_reservations (plan_id)
    WHERE plan_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS execution_plan_reservations_confirmation
    ON public.execution_plan_reservations (confirmation_id)
    WHERE confirmation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS execution_plan_reservations_attempt
    ON public.execution_plan_reservations (attempt_id)
    WHERE attempt_id IS NOT NULL;

-- Migration 009 made reservation linkage nullable for pre-cloud rows. MATCH
-- FULL preserves that legacy all-null form while rejecting partial new links.
-- The composite keys prevent a confirmation or attempt from another plan from
-- being attached to a reservation. Existing 009 application writes are either
-- all-null or internally consistent, so these validations are safe to perform.
DO $cloud_link_constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.execution_order_attempts'::regclass
          AND conname = 'execution_order_attempts_confirmation_plan_fk'
    ) THEN
        ALTER TABLE public.execution_order_attempts
            ADD CONSTRAINT execution_order_attempts_confirmation_plan_fk
            FOREIGN KEY (confirmation_id, plan_id)
            REFERENCES public.execution_confirmations (confirmation_id, plan_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.execution_plan_reservations'::regclass
          AND conname = 'execution_plan_reservations_attempt_plan_confirmation_fk'
    ) THEN
        ALTER TABLE public.execution_plan_reservations
            ADD CONSTRAINT execution_plan_reservations_attempt_plan_confirmation_fk
            FOREIGN KEY (attempt_id, plan_id, confirmation_id)
            REFERENCES public.execution_order_attempts
                (attempt_id, plan_id, confirmation_id)
            MATCH FULL;
    END IF;
END
$cloud_link_constraints$;
