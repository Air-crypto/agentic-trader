# Live Level 2 Options Contract

This contract governs listed-options trading in the agentic Robinhood account.
It supplements `AI_STOCK_PICKER.md` and `REAL_MONEY_EXECUTION.md`; every equity
guard remains active. Options are an expression of an evidence-grounded thesis,
not a substitute for one.

The account must be both `agentic_allowed` and approved for
`option_level_2`. Missing or lower access rejects every option order.

## Permitted strategies

Only these single-leg Level 2 strategies are eligible:

- long call;
- long put;
- covered call backed by 100 unencumbered shares per contract;
- cash-secured put backed by cash equal to strike times 100; and
- closing an existing position.

Naked short options, multi-leg spreads, stock-option combos, 0DTE, market
orders, early exercise, and holding through expiration are prohibited. A
covered call remains covered until its short contract is closed. Cash reserved
for a put remains unavailable until the short contract is closed.

## Initial live limits

- One contract per order.
- One opening options order per trading day.
- At most two open option positions.
- Entry expiration from 21 through 60 calendar days.
- Broker quote no older than 60 seconds.
- Positive bid and ask; spread no more than 10% of midpoint.
- Limit, good-for-day, regular-hours orders only.
- Long premium at risk is the lesser of $75 and 5% of current account equity.
- Aggregate open long premium is at most 10% of current equity.
- Cash-secured-put collateral is at most 30% of current equity.
- Assignment exposure must still satisfy the 15% issuer cap.
- The equity and option planners share the existing daily order-count controls.

These are ceilings, not targets. The current account size makes covered calls
and cash-secured puts rare: the system enables them only when 100-share coverage
or full collateral already fits every concentration and cash-reserve rule.

## Research and contract selection

Research may propose `long_call`, `long_put`, `covered_call`,
`cash_secured_put`, `close`, or `reject`. It supplies the thesis, catalyst,
counter-thesis, horizon, invalidation, and grounded evidence. It never chooses
an option identifier, strike, expiration, quantity, premium, or order type.

Execution resolves contracts from Robinhood in this order:

1. `get_option_chains`;
2. `get_option_instruments`;
3. `get_option_quotes`; and
4. deterministic filtering and ranking in repository code.

Delayed Yahoo option-chain data remains research-only and cannot authorize a
live order.

## Authorization and planning

An option order requires an immutable same-day option decision packet in
Postgres. The packet binds the research draft, critic verdict, contract,
strategy, maximum loss or collateral, quote timestamp, and structure
fingerprint. The planner re-reads the packet from Postgres and fails closed on
any hash, expiry, coverage, collateral, quote, or account mismatch.

Every approval carries exact parameters for `review_option_order` and
`place_option_order`. The automation passes them through unchanged. The
deterministic `ref_id` is derived from account hash, date, strategy, sorted leg
fingerprint, position effect, and intent; retries reuse it.

Covered shares and CSP cash are durably reserved only after every broker review
matches the plan and immediately before placement. `PLAN_ONLY` never reserves
resources. A terminal unfilled order releases its reservation during
`option-sync`; an ambiguous placement failure keeps the reservation and
requires reconciliation rather than assuming the order did not reach the
broker.

## Exits and assignment

Mandatory closes take priority over all entries. Buy-to-close or sell-to-close
is required on thesis invalidation, a 50% loss of premium for long calls/puts,
a short-option buyback ask at twice the opening credit, database or account
halt, operator rollback, or no later than five trading days before expiration.

Assignment or exercise is not treated as a normal fill. If broker state shows
assignment, exercise, an unknown option position, missing coverage, or released
collateral while a short contract remains open, halt new option entries and
escalate for reconciliation.

## Reconciliation

After placement, fetch both equity and option orders. Option reconciliation
matches the plan by `ref_id` and exact contract fingerprint, then checks
position effect, side, quantity, and net fill against the approved limit.
Unknown legs, duplicate fills, partial fills, or adverse fills outside tolerance
engage the repository kill switch and durable database halt.

No option lifecycle state changes until equity and option reconciliation are
both clean.

## Immediate-live rollout

Immediate live means no multi-day shadow period. It does not skip verification:
apply the options migration, run the complete test suite, execute one broker
`PLAN_ONLY` smoke with current chains and quotes, and only then permit one
guard-approved live order under the initial caps.
