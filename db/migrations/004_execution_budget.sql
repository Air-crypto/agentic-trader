CREATE TABLE IF NOT EXISTS execution_daily_usage (
    account_key TEXT NOT NULL,
    trade_date DATE NOT NULL,
    total_orders INTEGER NOT NULL DEFAULT 0 CHECK (total_orders >= 0),
    total_notional NUMERIC NOT NULL DEFAULT 0 CHECK (total_notional >= 0),
    entry_orders INTEGER NOT NULL DEFAULT 0 CHECK (entry_orders >= 0),
    entry_notional NUMERIC NOT NULL DEFAULT 0 CHECK (entry_notional >= 0),
    option_openings INTEGER NOT NULL DEFAULT 0 CHECK (option_openings >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_key, trade_date),
    CHECK (entry_orders <= total_orders),
    CHECK (entry_notional <= total_notional)
);

CREATE TABLE IF NOT EXISTS execution_plan_reservations (
    ref_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    trade_date DATE NOT NULL,
    notional NUMERIC NOT NULL CHECK (notional > 0),
    is_entry BOOLEAN NOT NULL,
    is_option_open BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (account_key, trade_date)
        REFERENCES execution_daily_usage(account_key, trade_date)
);

CREATE INDEX IF NOT EXISTS execution_plan_reservations_account_date
    ON execution_plan_reservations (account_key, trade_date);
