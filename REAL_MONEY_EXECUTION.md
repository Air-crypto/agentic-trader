# Real-Money Execution Contract

Governs live trading in the single agentic-enabled Robinhood account identified
by the `AGENTIC_TRADER_ACCOUNT` environment variable. Supersedes
`PAPER_TRADING_AGENT.md` for that account only. Every other account in the login
remains read-only and no rule here may be relaxed by prompt.

This repository is public. The account number is never committed; it is read
from the environment, and the guard rejects every order when it is unset. Live
state and audit logs live under the gitignored `artifacts/` directory and must
stay there.

## 1. What the research actually found

Read this before deciding how much to risk. No strategy in this repository has
passed its own pre-registered gates:

| Arm | Result | Verdict |
| --- | --- | --- |
| ETF momentum core | Sharpe 0.45, max drawdown −17.71% | Failed |
| Stock-inclusive variant | Survivorship-biased universe | Invalid |
| Tournament winner (`diversified_absolute_trend`) | Holdout drawdown −17.86% vs −10% mandate | Failed |
| Alternative-data event study | 4 events, t-stat 0.18 | Underpowered |
| LLM research arms (Sonnet 5, Grok 4.5) | Both rejected their own theses | No signal |

`artifacts/tournament/shadow-targets.json` is stamped `no_candidate_passes`. Live
trading is therefore **not** the deployment of a validated edge. It buys three
things: real fills, real slippage, and a working execution path. Size it as
tuition, not as an investment thesis.

The initial live allocation is a static 50/25/15 SPY/IEF/GLD split with 10% cash.
It is deliberately *not* one of the tested signal strategies, because none earned
the right to trade. It is a plain diversified portfolio that makes no alpha claim.

## 2. Enforcement is code, not instruction

`src/agentic_trader/execution.py` is the only sanctioned path to an order. It is
pure and deterministic, has no network access, and cannot place an order itself.
An agent may propose; only the guard may approve.

| Limit | Value | Rationale |
| --- | --- | --- |
| Account allowlist | `AGENTIC_TRADER_ACCOUNT` only | Unset rejects everything |
| Symbol allowlist | 11 liquid ETFs | Only instruments the backtest priced |
| Max order notional | $150 | One bad order cannot exceed 20% of the account |
| Max single-name weight | 25% | Concentration limit for individual issuers |
| Max broad-fund weight | 60% | Index funds are diversified; a stock is not |
| Cash reserve | 10% | Never fully invested |
| Max orders/day | 4 | Bounds a runaway loop |
| Max notional/day | $400 | Bounds a runaway loop in dollars |
| Daily loss halt | −3% | Stops trading into a bad session |
| Drawdown halt | −10% from high-water mark | Matches the original mandate |
| Capital floor | −10% of `AGENTIC_TRADER_NET_DEPOSITS` | Stateless; works with no persistence |
| No loss limit available | Rejects every order | Unprotected trading is worse than none |
| Unsettled deposits | Excluded from buying power | Cannot spend uncleared money |
| Order count source | Must be `broker` | A local count is not trustworthy |
| Session lock | `artifacts/live/session.lock` | Same-machine only; see below |
| Order idempotency | Deterministic `ref_id` per logical order | Broker collapses a duplicate pair |
| Daily budget | Broker history, floored by local state | A duplicate run cannot re-spend it |
| Kill switch | `KILL_SWITCH` file at repo root | Blocks every order unconditionally |

### Required environment

| Variable | Effect if unset |
| --- | --- |
| `AGENTIC_TRADER_ACCOUNT` | Every order rejected (`agentic_account_not_configured`) |
| `AGENTIC_TRADER_NET_DEPOSITS` | Capital floor disabled; without a persisted peak, every order rejected |

In Cursor Cloud these are set in the dashboard Secrets tab, since
`.cursor/environment.json` has no field for them. Set the account number as a
**Runtime Secret** rather than a plain Environment Variable so it is redacted
from transcripts, tool results, and commits. Update `AGENTIC_TRADER_NET_DEPOSITS`
whenever capital moves; a stale value silently moves the floor.

Automation memory must never hold risk state. It is a model-written file the
agent is permitted to delete mid-run, with no documented durability, size limit,
or concurrency semantics. Nothing in this contract depends on it.

### Duplicate triggers

This is a demonstrated failure mode, not a hypothetical: an automation run on
2026-08-09 observed a concurrent near-identical run writing to shared memory.
Two such runs would each read zero orders placed today and each submit the full
plan, doubling the position.

Cursor's documentation offers no exactly-once guarantee for scheduled triggers
and describes no mutual exclusion between concurrent runs of one automation, so
this must be assumed possible rather than designed against.

Three layers address it, and only the third works in the cloud:

1. **Session lock** (`artifacts/live/session.lock`) serializes runs on one
   machine. Two cloud runs execute on separate VMs with separate filesystems, so
   this protects a local scheduled job and nothing else. It is not a cloud
   control and must not be relied on as one.
2. **Broker order count** via `get_equity_orders` with `created_at_gte` set to
   today and `placed_agent="agentic"`. The broker sees a concurrent run's orders
   before either has written anything locally. This narrows the race to the
   window between query and placement but does not eliminate it. The guard
   rejects any snapshot whose `orders_source` is not `broker`.
3. **Deterministic `ref_id`**, which closes the window. Robinhood deduplicates on
   this key, so deriving it from the order's logical identity rather than from
   randomness means two runs computing the same plan submit the same key and the
   broker collapses them into one order. Every order in `approved_orders`
   carries its `ref_id` and it must be passed through to `place_equity_order`
   verbatim. Generating a fresh UUID instead reintroduces the double trade.

Local persisted consumption is retained as a floor under the broker count, since
an order accepted but not yet visible in history would otherwise read as zero.

Options, margin, and short selling are unreachable: the side allowlist is
`buy`/`sell`, and the account has no options level enabled.

The guard fails closed. A missing quote, a blank rationale, or an unknown state
produces a rejection rather than a market order.

## 3. Preventive versus detective control

The guard cannot physically block an order. It is a Python function, while order
placement is an MCP call, so an agent that ignores the guard reaches the broker
anyway. This is a real limitation and is not solved by instructing the agent more
firmly.

`src/agentic_trader/reconcile.py` is the compensating control. After every
session it compares actual fills against the approved plan and engages the kill
switch on any unauthorized fill, duplicate fill, oversized fill, or fill worse
than its limit price by more than 0.5%. An agent that bypasses the guard gets at
most one order through before trading halts, and the $150 order cap bounds what
that one order can cost.

Reconciliation is mandatory. An unattended session that places orders without
reconciling afterward is a contract violation, and skipping it removes the only
control that covers a bypassed guard.

The preventive gap closes properly only when placement moves into the same
process as the guard, which requires a direct broker API rather than MCP. Until
then, detection plus small caps is the honest ceiling on this design.

### Prompt injection

This agent reads news and web pages, which is untrusted input, and automations
run terminal commands without approval. Text on a page can therefore attempt to
instruct the agent. The controls that hold under that assumption are the ones an
instruction cannot alter: the symbol allowlist, the notional caps, the broker's
own refusal to trade any account without `agentic_allowed`, and reconciliation.
The controls that do not hold are anything phrased as guidance in this document.
Treat every limit here as a description of what the code enforces, not as the
enforcement itself.

## 4. Staged rollout

**Stage 1 — human approves every order.** The agent plans; a human places.

**Stage 2 — agent places guard-approved rebalances unattended (current stage).**
Permitted only for orders the guard approved from the written static allocation
in section 1. Every Stage 1 limit still applies, and reconciliation must run in
the same session as placement. A tripped kill switch ends unattended operation
until a human reviews and clears it.

**Stage 3 — signal-driven trading.** Blocked. Requires a strategy that passes its
holdout gates. Nothing currently qualifies, and Stage 3 does not open on a
schedule; it opens on evidence. Wanting to skip to Stage 3 is not evidence.

## 5. Running without local persistence

A cloud run starts from a fresh checkout, so `artifacts/live/state.json` is
absent and the high-water-mark drawdown halt cannot fire. The response is not to
externalize that file but to remove the dependency on it, because state stored
outside the broker can go stale, be lost, or be written by the wrong run.

Both critical protections are now derived rather than stored:

- **Daily budget** comes from broker order history, which is authoritative and
  already shared between concurrent runs.
- **Loss limit** comes from the capital floor, computed from
  `AGENTIC_TRADER_NET_DEPOSITS`, which is a constant rather than state.

The high-water-mark halt remains an additional check wherever the file exists,
and its absence degrades to the capital floor instead of to nothing. If neither
is available the guard rejects every order, so a misconfigured cloud run places
nothing rather than trading unprotected.

Update `AGENTIC_TRADER_NET_DEPOSITS` whenever capital is added or withdrawn. A
stale value silently moves the floor.

## 6. Session procedure

1. Stop immediately if `KILL_SWITCH` exists. Do not delete it.
2. Trade only on a regular session day. Skip weekends and market holidays.
3. Fetch account, positions, and quotes via the Robinhood MCP. Also call
   `get_equity_orders` with `created_at_gte` set to today and
   `placed_agent="agentic"`, and set `orders_source` to `broker`.
4. Write them to `artifacts/live/request.json` verbatim. Never edit a value to
   make an order pass; the high-water mark is read from disk precisely so a
   rewritten request cannot clear a halt.
5. Run `uv run agentic-trader live-plan --request artifacts/live/request.json --record-equity`.
6. Place **only** the orders in `approved_orders`, using each order's stated
   `limit_price`, notional, and `ref_id`. Never place an order the plan does not
   contain, even if it looks obviously correct, and never substitute a freshly
   generated `ref_id` for the one in the plan.
7. Wait for terminal order states, then fetch actual fills with
   `get_equity_orders` and write them to `artifacts/live/executed.json`.
8. Run `uv run agentic-trader live-reconcile --executed artifacts/live/executed.json`.
9. If reconciliation is not clean, stop and escalate. The kill switch is already
   engaged at that point and only a human may clear it.

## 7. Stop conditions

Halt and escalate to a human on any of: guard rejection the agent does not
understand, a fill more than 0.5% from the limit price, a position appearing that
no plan authorized, MCP authentication failure, a `live-plan` exit code of 3
(another session holds the lock — do not retry, do not delete the lock), or two
consecutive sessions where the audit log fails to reconcile.

## 8. Prohibited

Editing limits to make a rejected order pass. Trading any account other than the
configured one. Generating a `ref_id` instead of using the plan's. Storing risk
state in automation memory. Committing the account number, balances, or live
state to this public repository. Placing orders directly through the MCP without a guard-approved
plan. Placing orders without reconciling in the same session. Deleting or
clearing `KILL_SWITCH` without human review. Options, margin, shorting, or any
symbol off the allowlist. Deleting or rewriting `artifacts/live/audit.jsonl`.
Treating a guard approval as a prediction of profit — it certifies bounded risk
and nothing else.
