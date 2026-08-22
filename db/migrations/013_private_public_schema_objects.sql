-- Supabase exposes the public schema through PostgREST. Table RLS alone is
-- insufficient for an owner-executing view, a sequence, and trigger functions.
-- Revoke only the exact application objects that predate the private-object
-- migrations. Future migrations run atomically and the runtime catalog
-- attestation rolls the transaction back if any new object retains an API grant.
REVOKE ALL ON TABLE public.learning_current_state
    FROM PUBLIC, anon, authenticated, service_role;

REVOKE ALL ON SEQUENCE public.picker_order_events_event_id_seq
    FROM PUBLIC, anon, authenticated, service_role;

REVOKE EXECUTE ON FUNCTION public.reject_option_packet_content_update()
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION public.reject_pending_research_content_update()
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION public.reject_learning_row_mutation()
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION public.validate_learning_prediction_insert()
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION public.validate_complete_learning_batch()
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION public.validate_learning_outcome_insert()
    FROM PUBLIC, anon, authenticated, service_role;
REVOKE EXECUTE ON FUNCTION public.validate_learning_promotion_insert()
    FROM PUBLIC, anon, authenticated, service_role;

COMMENT ON VIEW public.learning_current_state IS
    'Private derived learning state; only the direct database owner may read it.';
