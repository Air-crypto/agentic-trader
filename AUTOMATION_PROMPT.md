# Automation Runbook

## Canonical tasks

The deployment uses two local-wall-clock tasks, both in `America/Los_Angeles`:

| Task | Definition | Prompt | Schedule | Execution window |
| --- | --- | --- | --- | --- |
| Morning live | [`automations/morning-live.json`](automations/morning-live.json) | [`automations/morning-live-prompt.txt`](automations/morning-live-prompt.txt) | Weekdays at `06:35` | Regular hours |
| Evening live | [`automations/evening-live.json`](automations/evening-live.json) | [`automations/evening-live-prompt.txt`](automations/evening-live-prompt.txt) | Sunday-Thursday at `18:15` | Robinhood all-day hours when eligible |

[`automation.json`](automation.json) is the root manifest. Research and criticism run inside the two canonical tasks; there is no separate execution task.

## What each scheduled turn may do

1. Load configuration and reconcile Robinhood account, positions, open orders, fills, buying power, and session.
2. Fail closed on the kill switch, stale or missing data, broker uncertainty, risk breach, or account mismatch.
3. Run point-in-time research and a separate independent critic.
4. Treat social sources as discovery or sentiment only. Require a registered-issuer or exchange primary source for every actionable thesis. SEC ingestion is disabled.
5. Apply deterministic allocation, loss, drawdown, and entry limits.
6. Create an equity order plan that expires in five minutes.
7. Request Robinhood's nonplacing order review.
8. Show the exact reviewed order and ask the user to confirm it.
9. Stop without reservation or placement.

No scheduled task opens an option or changes a current/manual holding without a separately authorized close.

## Mandatory confirmation handoff

The review message must include the exact symbol, side, share quantity or dollar amount, order type, limit price when applicable, time in force, session, notional, plan ID, and expiry. A schedule firing, `MODE=live`, standing consent, or a previous confirmation never counts as confirmation of the displayed order.

If the user explicitly confirms that exact order, a resumed turn must first re-read the plan, broker state, account state, and quote. It may reserve and place only when every displayed field remains unchanged and the five-minute plan is still valid. Otherwise it must regenerate, re-review, show the replacement order, and ask again.

After placement, reconcile by client order ID. An ambiguous timeout is a reconciliation problem, not permission to submit again.

## Morning constraints

- regular-hours equities only;
- live-canary portfolio limits remain binding;
- at most two total new entries and `$300` aggregate new-entry notional across the trading day;
- exact confirmation required for every proposed entry or close.

## Evening constraints

- at most one new opening proposal;
- at most `$100` notional;
- Robinhood must report all-day-hours eligibility;
- fresh quote and spread no wider than `10 bps`;
- whole-share GFD limit order only;
- no proposal when eligibility, spread, session, or quote freshness is uncertain.

## Live-canary limits

- `3` concurrent names;
- `3.5%` per name;
- `7%` per sector;
- `89.5%` minimum cash;
- `2` entries and `$300` new-entry notional per day;
- `0.5%` daily-loss halt;
- `3%` drawdown halt.

## Telemetry

Candidate, reject, critic, counterfactual-outcome, and knowledge-graph records are best-effort telemetry. A write failure should be logged, but it must not change authorization, expand exposure, or become a promotion requirement.

## Required operations

- Keep `DATABASE_URL` and `AGENTIC_TRADER_NET_DEPOSITS` configured.
- Keep broker and model secrets out of prompts, logs, and source control.
- Create a root-level `KILL_SWITCH` file to block new review, reservation, and placement.
- Inspect both task definitions after any schedule or prompt change; the filenames above are the production task identities.
