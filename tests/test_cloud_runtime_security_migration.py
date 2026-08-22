from pathlib import Path

MIGRATION = Path("db/migrations/010_cloud_runtime_security.sql")
LEGACY_MIGRATION = Path("db/migrations/011_private_application_tables.sql")
SCHEMA_OBJECT_MIGRATION = Path("db/migrations/013_private_public_schema_objects.sql")

SENSITIVE_TABLES = (
    "schema_migrations",
    "automation_runs",
    "execution_plans",
    "execution_plan_reviews",
    "execution_confirmations",
    "execution_order_attempts",
    "execution_attempt_transitions",
    "execution_reconciliations",
    "execution_audit_events",
    "cloud_runtime_artifacts",
    "knowledge_nodes",
    "knowledge_edges",
    "knowledge_observations",
    "execution_plan_reservations",
)


def test_cloud_runtime_security_migration_is_private_and_fail_closed() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for table in SENSITIVE_TABLES:
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;" in sql
        assert f"COMMENT ON TABLE public.{table}" in sql

    assert "FROM PUBLIC;" in sql
    assert "ARRAY['anon', 'authenticated', 'service_role']" in sql
    assert "CREATE POLICY" not in sql
    assert "FORCE ROW LEVEL SECURITY" not in sql


def test_cloud_reservation_links_are_indexed_and_identity_consistent() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "execution_plan_reservations_plan" in sql
    assert "execution_plan_reservations_confirmation" in sql
    assert "execution_plan_reservations_attempt" in sql
    assert "execution_order_attempts_confirmation_plan_fk" in sql
    assert "execution_plan_reservations_attempt_plan_confirmation_fk" in sql
    assert "MATCH FULL" in sql


def test_every_application_table_is_closed_to_supabase_api_roles() -> None:
    create_sql = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("db/migrations").glob("*.sql")
    )
    security_sql = "\n".join(
        (MIGRATION.read_text(encoding="utf-8"), LEGACY_MIGRATION.read_text(encoding="utf-8"))
    )
    tables = {
        line.split("CREATE TABLE IF NOT EXISTS ", 1)[1].split(" ", 1)[0].split("(", 1)[0]
        for line in create_sql.splitlines()
        if line.startswith("CREATE TABLE IF NOT EXISTS ")
    }

    for table in tables:
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;" in security_sql

    assert "ARRAY['anon', 'authenticated', 'service_role']" in security_sql
    assert "CREATE POLICY" not in security_sql


def test_every_legacy_public_schema_object_is_explicitly_private() -> None:
    sql = SCHEMA_OBJECT_MIGRATION.read_text(encoding="utf-8")

    assert "REVOKE ALL ON TABLE public.learning_current_state" in sql
    assert "REVOKE ALL ON SEQUENCE public.picker_order_events_event_id_seq" in sql
    for function_name in (
        "reject_option_packet_content_update",
        "reject_pending_research_content_update",
        "reject_learning_row_mutation",
        "validate_learning_prediction_insert",
        "validate_complete_learning_batch",
        "validate_learning_outcome_insert",
        "validate_learning_promotion_insert",
    ):
        assert f"REVOKE EXECUTE ON FUNCTION public.{function_name}()" in sql
    assert "ALTER DEFAULT PRIVILEGES" not in sql
    assert "COMMENT ON VIEW public.learning_current_state" in sql
