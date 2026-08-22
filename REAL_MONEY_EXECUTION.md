# Real-Money Execution

## Deployment status

Real-money operation is authorized only as a small, human-confirmed equity canary. The twice-daily tasks may complete research, deterministic authorization, plan construction, and Robinhood order review. They may not reserve or place a concrete order until the user confirms that exact reviewed order.

The workflow is intentionally fail-closed. Missing data, stale quotes, uncertain broker state, an expired plan, a risk breach, or ambiguous consent means no order.

## Human confirmation boundary

The scheduled run must stop after broker review and present, at minimum:

- symbol and side;
- exact share quantity or dollar amount;
- order type and limit price;
- time in force and trading session;
- maximum notional;
- current quote and spread;
- plan ID and five-minute expiry time;
- the deterministic reasons the order passed or failed.

Only an explicit reply confirming that exact order authorizes a resumed run to reserve and place it. A schedule, `MODE=live`, prior consent, a standing instruction, or approval of a different price or quantity is not sufficient.

Before acting on confirmation, the resumed run must re-read the plan, account, positions, open orders, quote, and market session. If five minutes have elapsed or any material order input has changed, the old confirmation is void. Regenerate the plan, obtain a new Robinhood review, show the new exact order, and ask again.

## Portfolio and loss limits

| Control | Live-canary ceiling |
| --- | ---: |
| Concurrent names | `3` |
| Per-name allocation | `3.5%` of equity |
| Per-sector allocation | `7%` of equity |
| Minimum cash reserve | `89.5%` |
| New entries per day | `2` |
| Aggregate new-entry notional per day | `$300` |
| Daily loss halt | `0.5%` |
| Peak-to-trough drawdown halt | `3%` |

These limits are enforced together with the engine's per-order and broker constraints; the strictest result wins. They cannot be relaxed by model output, telemetry, or user confirmation of a single order.

Crossing a loss or drawdown limit blocks new entries. It does not permit an automatic sale of an existing or manual holding.

## Session rules

### Morning task

[`automations/morning-live.json`](automations/morning-live.json) operates during regular market hours. It may produce reviewed equity entry or close proposals that satisfy the deterministic controls, but each concrete order still requires its own confirmation.

### Evening task

[`automations/evening-live.json`](automations/evening-live.json) may propose at most one new opening order. The proposal must be:

- a supported equity, not an option;
- at most `$100` notional;
- a whole-share limit order with GFD time in force;
- explicitly eligible for Robinhood all-day hours;
- based on a fresh quote with spread at or below `10 bps`.

If any condition is absent or uncertain, the evening result is research-only and no order may be reserved.

## Holdings and exits

Current holdings, including positions opened manually, are preserved by default. Research, a risk flag, or a model recommendation does not authorize a close. A close needs a fresh deterministic plan, broker review, and explicit confirmation of the exact close order.

The scheduled tasks do not open options. They also do not close, cancel, roll, or exercise an existing option automatically.

## Transactional lifecycle

1. Reconcile broker positions, orders, fills, buying power, and session.
2. Create a deterministic plan with a five-minute expiry.
3. Request the broker's nonplacing order review.
4. Display the exact order and wait for user confirmation.
5. On a resumed run, validate that the plan and confirmation are still current.
6. Reserve risk limits transactionally.
7. Place once with a stable client order ID.
8. Reconcile the broker response; never retry blindly after an ambiguous timeout.
9. Append the decision, confirmation, reservation, placement, and reconciliation events.

Reservation and idempotency reduce concurrency risk, but the broker is authoritative. If local and broker state disagree, stop new activity until reconciliation is complete.

## Evidence and model boundaries

Social feeds may identify a candidate or measure sentiment, but they cannot substantiate an actionable thesis. A trade thesis needs a registered-issuer or exchange primary source, current enough for the decision time. SEC ingestion is disabled, so lack of an alternate qualifying primary source means no actionable candidate.

The analyst and separate critic may rank or reject candidates. Neither may change quantities, override deterministic limits, or authorize broker action.

Counterfactual learning and knowledge-graph writes are best-effort, nonblocking telemetry. They do not decide whether an order is allowed and are not a promotion condition.

## Operator checklist

Before confirming an order:

1. Check that the displayed symbol, side, quantity, price, session, and notional are exactly intended.
2. Check the plan expiry and quote timestamp.
3. Confirm the account and buying-power figures match Robinhood.
4. Confirm that no current/manual holding will be changed unintentionally.
5. Confirm only that one exact order; do not use a general authorization phrase.

Create a root-level `KILL_SWITCH` file whenever trading should stop. Keep `DATABASE_URL` and `AGENTIC_TRADER_NET_DEPOSITS` configured, and never store broker or model credentials in source control.
