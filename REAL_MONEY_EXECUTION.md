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

Only a later reply carrying a valid Ed25519 signature over that exact plan ID and review hash authorizes a resumed run to reserve and place it. A schedule, `MODE=live`, prior consent, a standing instruction, or approval of a different price or quantity is not sufficient. The private key stays on the user's Mac.

Before acting on confirmation, the resumed run must re-read the plan, account, positions, open orders, quote, and market session. The unchanged reviewed broker parameters are re-evaluated under current durable usage and risk controls. If five minutes have elapsed or fresh state no longer authorizes them, the old confirmation is void. Regenerate the plan, obtain a new Robinhood review, show the new exact order, and ask again.

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

Current equity holdings, including positions opened manually, are preserved by default. Research, a risk flag, or a model recommendation does not authorize an equity close. An equity close needs a fresh deterministic plan, broker review, and explicit confirmation of the exact close order.

Option positions and orders are read-only broker-truth inputs for reconciliation and equity risk checks. The scheduled tasks do not create option drafts or authorize, plan, review, close, cancel, roll, exercise, or otherwise mutate an option. The repository retains tested exact-batch close-only CLI primitives, but no option-close workflow is activated or supported by these scheduled tasks.

## Transactional lifecycle

1. Pass the read-only migration checksum check and acquire a durable scheduled-window lease.
2. Reconcile broker positions, orders, fills, buying power, session, and every nonterminal Supabase attempt.
3. Persist the broker-snapshot hash, redacted artifacts, account hash, and deterministic five-minute plan in Supabase.
4. Request the broker's nonplacing order review and persist the exact `order_checks`, full `quote_data`, verbatim market-data disclosure, complete native response, and exact request parameters under a review hash.
5. Display the exact order and a local `confirmation-sign` command. Wait for its one-line `CONFIRM <plan_id> <review_hash> SIGNATURE <signature>` output in a later user turn.
6. On the resumed turn, cryptographically verify and persist that exact confirmation, then revalidate expiry, identity, plan hash, broker state, session, usage, risk, and quotes. A failed revalidation requires a new review and signature.
7. Persist a prepared attempt, reserve risk limits transactionally, and return exact broker parameters only for a newly safe attempt.
8. Mark the attempt `submitting`, place once with its stable client order ID, then immediately persist `submitted` or `unknown`.
9. Reconcile broker truth; never retry an unknown result. Persist the result, transition terminal attempts, and append every audit event in Supabase.

The database, not a local artifact, carries the plan/confirmation/attempt handshake across VMs. Reservation and idempotency reduce concurrency risk, but Robinhood remains authoritative. If database and broker state disagree, an all-order durable halt blocks new activity until reconciliation is complete.

## Evidence and model boundaries

Social feeds may identify a candidate or measure sentiment, but they cannot substantiate an actionable thesis. A trade thesis needs a registered-issuer or exchange primary source, current enough for the decision time. SEC ingestion is disabled, so lack of an alternate qualifying primary source means no actionable candidate.

`gpt-5.6-sol` is the only configured research and draft-generation model. There is no independent critic or self-critic step, and no critic verdict should be fabricated. Its output passes directly to deterministic guards for issuer/exchange authority, not-in-future timestamps, quote grounding, quantitative freshness, portfolio limits, and execution constraints. Those guards do not detect semantically conflicting evidence or judge catalyst freshness. That independent challenge is intentionally absent: the sole analyst records its counter-thesis and identified contradictions, and the human should scrutinize unresolved conflicts. The model may rank or reject candidates, but it may not change quantities, override deterministic limits, or authorize broker action.

Counterfactual learning and knowledge-graph writes are best-effort, nonblocking telemetry. They do not decide whether an order is allowed and are not a promotion condition.

## Operator checklist

Before confirming an order:

1. Check that the displayed symbol, side, quantity, price, session, and notional are exactly intended.
2. Check the plan expiry and quote timestamp.
3. Confirm the account and buying-power figures match Robinhood.
4. Confirm that no current/manual holding will be changed unintentionally.
5. Confirm only that one exact order; do not use a general authorization phrase.

Use the durable database halt whenever cloud trading should stop; a reconciliation breach engages its all-order scope automatically. A root-level `KILL_SWITCH` is an additional local override, not cloud authority. Keep `DATABASE_URL`, `AGENTIC_TRADER_NET_DEPOSITS`, and `AGENTIC_TRADER_CONFIRMATION_PUBLIC_KEY` in the runtime secret store. Keep the matching private key only on the user's Mac, and never store broker or model credentials in source control.

The repository cannot physically intercept direct calls to a write-capable Robinhood MCP. For a hard order boundary, scheduled automation must receive only read/review broker tools; placement must be exposed through a separate user-triggered executor or proxy that verifies the signature and newly acquired database claim. If all Robinhood tools are attached to one Cursor automation, no-placement remains a prompt-enforced rule rather than a tool-permission guarantee.

Inspect the durable controls with `live-control-status --snapshot <native-broker-snapshot>` and engage an emergency stop with `live-halt --snapshot <native-broker-snapshot> --scope all --reason <reason>`. There is deliberately no automation-callable resume command; clearing an all-order halt requires a separately reviewed operator/database procedure after broker reconciliation.
