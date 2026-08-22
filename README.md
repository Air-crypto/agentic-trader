# Agentic Trader

Agentic Trader implements a bounded **live canary** workflow twice each trading day. It automates research, a deterministic equity plan, and Robinhood order review. The supported execution path requires a fresh human-held Ed25519 signature over every exact review before reservation or placement. Scheduled prompts forbid placement; see the broker-tool boundary below before activation.

This is an experimental system, not a promise of profit. The strategy's unvalidated edge is a reason to keep the canary small, preserve cash, and treat every proposed trade as optional.

## Current operating contract

- Tasks: [`automations/morning-live.json`](automations/morning-live.json) and [`automations/evening-live.json`](automations/evening-live.json).
- Cursor UI schedules, Runtime Secrets, MCP, and copy-ready instruction wrappers: [`automations/CURSOR_SETUP.md`](automations/CURSOR_SETUP.md).
- Model: `gpt-5.6-sol` is the sole research and draft-generation model. No independent critic or self-critic run is configured. Deterministic guards do not detect semantic evidence conflicts or judge catalyst freshness, so the analyst must expose its counter-thesis and identified contradictions for human scrutiny.
- Morning: regular-hours equity candidates only.
- Evening: at most one new opening, at most `$100`, and only when Robinhood marks the equity eligible for all-day hours, the spread is at most `10 bps`, and a fresh whole-share GFD limit order can be formed.
- The system may research, authorize, plan, and review an equity order automatically.
- Before any reserve or place action, it must show the exact current equity order and ask the user to confirm that order.
- A schedule, mode setting, earlier approval, or general instruction to trade is never order confirmation.
- A plan expires five minutes after creation. Reservation revalidates the unchanged reviewed order against fresh broker state, durable usage, risk controls, and quotes no older than 15 seconds; failed revalidation requires a new plan, review, and signature.
- Option positions and orders are read-only broker-truth inputs for these scheduled tasks. The tasks do not create option drafts or authorize, plan, review, or mutate options. The repository retains tested exact-batch close-only CLI primitives, but no option-close workflow is activated or supported by either scheduled automation.
- No shorts, margin, leveraged ETFs, averaging down, or same-day re-entry.
- Existing and manually opened equity holdings are preserved unless the user separately authorizes a specific equity close.

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
5. Pass the analyst drafts directly through deterministic source, timestamp, quantitative, and portfolio authorization; no critic verdict is generated, and human review must scrutinize unresolved semantic conflicts.
6. Build a five-minute equity plan and request a Robinhood order review.
7. Show the exact symbol, side, quantity, order type, limit price, time in force, session, notional, and plan expiry.
8. Wait for the user's local signature of that exact order. Only a fresh resumed run that verifies the signature may reserve and place it.
9. Reconcile the broker result and append the audit record.

## Learning and knowledge graph

Predictions, rejected candidates, counterfactual outcomes, evidence, and knowledge-graph links can be recorded in Supabase when their required inputs are available. Runtime KG nodes, relationships, and immutable supporting/contradicting observations survive fresh cloud VMs; checked-in Markdown is a curated schema/example export. This telemetry is **nonblocking**: it cannot change an authorization decision, expand risk, or become a promotion gate.

## Cloud runtime and recovery

Cursor Automation VMs are disposable. Each run must pass `cloud-schema-check`, acquire a schedule-window lease, reconcile every nonterminal order attempt, and persist redacted content-addressed inputs. A live plan stores the broker-snapshot hash, exact order parameters, immutable limits, five-minute expiry, and account hash in Supabase; raw account identifiers are not stored in the plan. Robinhood review output is bound to a second hash, and only a valid Ed25519 signature of `CONFIRM <plan_id> <review_hash>` can create a confirmation.

Before a broker call, `live-reserve` persists an attempt and transactionally reserves the daily budget. The agent marks the attempt `submitting` before placement and records `submitted` or `unknown` immediately afterward. A fresh VM blocks on any unresolved attempt and reconciles through the persisted Robinhood `broker_order_id` using native order lookup. Robinhood order-list rows do not expose the client `ref_id`; an ambiguous submission with no returned broker ID remains halted for manual reconciliation and is never retried.

## Safety controls

- A durable database halt survives cloud restarts. Reconciliation breaches use an all-order scope; ordinary risk halts block entries while still allowing a separately reviewed reducing equity exit. A root-level `KILL_SWITCH` remains an additional local stop only.
- `DATABASE_URL` is required for durable plans, reservations, idempotency, and audit history.
- `AGENTIC_TRADER_NET_DEPOSITS` is required for loss-from-deposits and drawdown checks.
- `AGENTIC_TRADER_CONFIRMATION_PUBLIC_KEY` verifies user confirmations; its private half stays only on the user's Mac.
- Robinhood credentials are supplied through the configured broker connection; never commit secrets.
- `SEC_USER_AGENT` is not required because SEC ingestion is disabled for the scheduled workflow.
- Reservation is transactional and placement is idempotent by client order ID.
- Broker reconciliation is authoritative after timeouts or ambiguous responses.

Cursor attaches every tool exposed by an MCP server. The signed CLI flow prevents accidental auto-confirmation and concurrent double claims, but repository code cannot physically stop a scheduled agent from bypassing it and calling an attached write-capable Robinhood tool directly. A hard boundary therefore requires read/review-only broker tools on scheduled runs and a separate signed executor/proxy for placement. That executor must fetch and paginate broker truth itself; agent-authored JSON cannot prove order-history completeness. The repository does not deploy this proxy. Without that tool split, the prompt's no-placement rule is behavioral and the schedules should remain research/review-only unless that residual risk is explicitly accepted.

## Local setup

```bash
uv sync
uv run agentic-trader cloud-migrate
uv run agentic-trader cloud-schema-check
uv run agentic-trader learning-status
```

Run `cloud-migrate` only as a reviewed deployment action. Scheduled automations perform the read-only schema check and must never apply migrations themselves.

Run the focused test suite before changing execution behavior:

```bash
uv run pytest
uv run ruff check .
```

Operational details are in [REAL_MONEY_EXECUTION.md](REAL_MONEY_EXECUTION.md), picker behavior in [AI_STOCK_PICKER.md](AI_STOCK_PICKER.md), and the option prohibition in [OPTION_EXECUTION.md](OPTION_EXECUTION.md).
