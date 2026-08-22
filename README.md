# Agentic Trader

Agentic Trader now runs a bounded **live canary** workflow twice each trading day. It automates research, a deterministic equity plan, and Robinhood order review. A scheduled run cannot place an order on its own: every concrete order requires a fresh, exact user confirmation before reservation or placement.

This is an experimental system, not a promise of profit. The strategy's unvalidated edge is a reason to keep the canary small, preserve cash, and treat every proposed trade as optional.

## Current operating contract

- Tasks: [`automations/morning-live.json`](automations/morning-live.json) and [`automations/evening-live.json`](automations/evening-live.json).
- Morning: regular-hours equity candidates only.
- Evening: at most one new opening, at most `$100`, and only when Robinhood marks the equity eligible for all-day hours, the spread is at most `10 bps`, and a fresh whole-share GFD limit order can be formed.
- The system may research, authorize, plan, and review an order automatically.
- Before any reserve or place action, it must show the exact current order and ask the user to confirm that order.
- A schedule, mode setting, earlier approval, or general instruction to trade is never order confirmation.
- A plan expires five minutes after creation. Any expiry, quote change, account change, order change, or stale review requires a new plan, review, and confirmation.
- No new option orders, shorts, margin, leveraged ETFs, averaging down, or same-day re-entry.
- Existing and manually opened holdings are preserved unless the user separately authorizes a specific close.

## Live-canary limits

All limits are hard ceilings, not targets:

| Control | Limit |
| --- | ---: |
| Concurrent names | `3` |
| Per-name allocation | `3.5%` of equity |
| Per-sector allocation | `7%` of equity |
| Minimum cash reserve | `89.5%` |
| New entries per trading day | `2` |
| New-entry notional per day | `$300` |
| Daily loss halt | `0.5%` |
| Drawdown halt | `3%` |

The stricter of these portfolio limits and the execution engine's per-order limits applies. A halt blocks new entries; it does not authorize an automatic liquidation.

## Twice-daily flow

1. Reconcile Robinhood orders, fills, positions, buying power, and market session.
2. Stop if the kill switch, loss limit, drawdown limit, data freshness, or broker-state checks fail.
3. Research the candidate universe using point-in-time data.
4. Use social sources only for discovery or sentiment. An actionable thesis must be supported by a registered issuer or exchange primary source. SEC ingestion is disabled in this deployment.
5. Run a separate critic and deterministic portfolio authorization.
6. Build a five-minute equity plan and request a Robinhood order review.
7. Show the exact symbol, side, quantity, order type, limit price, time in force, session, notional, and plan expiry.
8. Wait for explicit confirmation of that exact order. Only a fresh resumed run may reserve and place it.
9. Reconcile the broker result and append the audit record.

## Learning and knowledge graph

Predictions, rejected candidates, counterfactual outcomes, evidence, and knowledge-graph links are recorded when available. This telemetry is append-only and useful for diagnosis, but it is **nonblocking**: telemetry failure must not change an authorization decision, expand risk, or become a prerequisite for the live canary. It is not a promotion gate.

## Safety controls

- A root-level `KILL_SWITCH` file blocks new broker review, reservation, and placement.
- `DATABASE_URL` is required for durable plans, reservations, idempotency, and audit history.
- `AGENTIC_TRADER_NET_DEPOSITS` is required for loss-from-deposits and drawdown checks.
- Robinhood credentials are supplied through the configured broker connection; never commit secrets.
- `SEC_USER_AGENT` is not required because SEC ingestion is disabled for the scheduled workflow.
- Reservation is transactional and placement is idempotent by client order ID.
- Broker reconciliation is authoritative after timeouts or ambiguous responses.

## Local setup

```bash
uv sync
uv run agentic-trader option-migrate
uv run agentic-trader learning-status
```

Run the focused test suite before changing execution behavior:

```bash
uv run pytest
uv run ruff check .
```

Operational details are in [REAL_MONEY_EXECUTION.md](REAL_MONEY_EXECUTION.md), picker behavior in [AI_STOCK_PICKER.md](AI_STOCK_PICKER.md), and the option prohibition in [OPTION_EXECUTION.md](OPTION_EXECUTION.md).
