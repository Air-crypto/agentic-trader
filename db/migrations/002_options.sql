CREATE TABLE IF NOT EXISTS option_decision_packets (
    packet_id TEXT PRIMARY KEY,
    valid_for_date DATE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    packet_hash TEXT NOT NULL UNIQUE CHECK (length(packet_hash) = 64),
    structure_fingerprint TEXT NOT NULL
        CHECK (length(structure_fingerprint) = 64),
    status TEXT NOT NULL DEFAULT 'authorized'
        CHECK (status IN ('authorized', 'consumed', 'revoked')),
    authorized_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revocation_reason TEXT,
    payload JSONB NOT NULL,
    CHECK (
        (
            status = 'authorized'
            AND consumed_at IS NULL
            AND revoked_at IS NULL
            AND revocation_reason IS NULL
        )
        OR (
            status = 'consumed'
            AND consumed_at IS NOT NULL
            AND revoked_at IS NULL
            AND revocation_reason IS NULL
        )
        OR (
            status = 'revoked'
            AND consumed_at IS NULL
            AND revoked_at IS NOT NULL
            AND revocation_reason IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS option_decision_packets_valid
    ON option_decision_packets (valid_for_date, expires_at)
    WHERE status = 'authorized';

CREATE UNIQUE INDEX IF NOT EXISTS one_authorized_option_structure_per_day
    ON option_decision_packets (valid_for_date, structure_fingerprint)
    WHERE status = 'authorized';

CREATE OR REPLACE FUNCTION reject_option_packet_content_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.packet_id IS DISTINCT FROM OLD.packet_id
        OR NEW.valid_for_date IS DISTINCT FROM OLD.valid_for_date
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        OR NEW.packet_hash IS DISTINCT FROM OLD.packet_hash
        OR NEW.structure_fingerprint IS DISTINCT FROM OLD.structure_fingerprint
        OR NEW.authorized_at IS DISTINCT FROM OLD.authorized_at
        OR NEW.payload IS DISTINCT FROM OLD.payload
    THEN
        RAISE EXCEPTION 'option packet content is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_trigger
        WHERE tgname = 'option_packet_content_is_immutable'
          AND tgrelid = 'option_decision_packets'::regclass
    ) THEN
        CREATE TRIGGER option_packet_content_is_immutable
        BEFORE UPDATE ON option_decision_packets
        FOR EACH ROW
        EXECUTE FUNCTION reject_option_packet_content_update();
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS active_option_positions (
    position_id TEXT PRIMARY KEY,
    packet_id TEXT NOT NULL REFERENCES option_decision_packets(packet_id),
    underlying TEXT NOT NULL,
    strategy TEXT NOT NULL
        CHECK (
            strategy IN (
                'long_call',
                'long_put',
                'covered_call',
                'cash_secured_put'
            )
        ),
    status TEXT NOT NULL
        CHECK (
            status IN (
                'pending_open',
                'open',
                'closing',
                'closed',
                'expired',
                'assigned',
                'exercised',
                'halted'
            )
        ),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS active_option_positions_status_underlying
    ON active_option_positions (status, underlying);

CREATE TABLE IF NOT EXISTS option_resource_reservations (
    packet_id TEXT PRIMARY KEY REFERENCES option_decision_packets(packet_id),
    account_key TEXT NOT NULL,
    collateral_amount NUMERIC NOT NULL DEFAULT 0
        CHECK (collateral_amount >= 0),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'released')),
    reserved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at TIMESTAMPTZ,
    CHECK (
        (status = 'active' AND released_at IS NULL)
        OR (status = 'released' AND released_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS option_resource_reservations_account
    ON option_resource_reservations (account_key, status);

CREATE TABLE IF NOT EXISTS option_share_encumbrances (
    packet_id TEXT NOT NULL
        REFERENCES option_resource_reservations(packet_id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (packet_id, symbol)
);

CREATE INDEX IF NOT EXISTS option_share_encumbrances_symbol
    ON option_share_encumbrances (symbol);

CREATE TABLE IF NOT EXISTS option_order_events (
    event_id TEXT PRIMARY KEY,
    packet_id TEXT REFERENCES option_decision_packets(packet_id),
    position_id TEXT REFERENCES active_option_positions(position_id),
    ref_id TEXT,
    broker_order_id TEXT,
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS option_order_events_packet_time
    ON option_order_events (packet_id, occurred_at);

CREATE INDEX IF NOT EXISTS option_order_events_ref_id
    ON option_order_events (ref_id)
    WHERE ref_id IS NOT NULL;
