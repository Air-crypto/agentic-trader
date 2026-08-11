# Automation Operating Prompts

Stages 3 and 4 use two Cursor Automations. Keep these files in sync with
`AI_STOCK_PICKER.md`, `OPTION_EXECUTION.md`, and
`REAL_MONEY_EXECUTION.md`. Where they disagree, the contracts win and the run
should stop and say so.

Cursor Automation cron is **UTC only** (no timezone field). Schedules below are
chosen so the live session is inside the US cash equity regular session
(09:30–16:00 America/New_York) in both EDT and EST.

| Automation | Cron (UTC) | Eastern time | Prompt source |
| --- | --- | --- | --- |
| AI Picker Research | `0 12 * * 1-5` | 08:00 EDT / 07:00 EST (pre-open) | `automations/research-prompt.txt` |
| Independent Grok Critic | `0 14 * * 1-5` | 10:00 EDT / 09:00 EST | `automations/critic-prompt.txt` |
| AI Picker Live Session | `0 15 * * 1-5` | 11:00 EDT / 10:00 EST (RTH) | `automations/execution-prompt.txt` |

`15:00` UTC is always at least 30 minutes after the open and well before the
close year-round. Do not use a UTC hour that falls before 09:30 ET in winter
(for example `14:00` UTC is 09:00 EST — too early for fractional LIVE orders).

Wire JSON for Cursor is in `automations/research.json`,
`automations/critic.json`, and `automations/execution.json`. Root
`automation.json` / `automation-prompt.txt` mirror execution. Repo JSON does not
push schedules into the product UI by itself.

Disable the old static SPY/IEF/GLD automation in the Cursor UI so two live
sessions do not compete for the daily $400 / 4-order budget.

## Research

No Robinhood MCP. Requires `DATABASE_URL` set to the Supabase Shared Pooler URI
from the Connect panel (`*.pooler.supabase.com`, user `postgres.<project-ref>`).
Direct `db.*.supabase.co` hosts fail with `Network is unreachable` (IPv6-only);
a pooler host with user `postgres` fails password auth. Stages verified
evidence plus stock and option drafts via `picker-stage-pending`. Research never
criticizes itself, chooses option contracts, or places orders.

## Independent critic

Runs on Grok with no Robinhood tools. It exports the pending Sonnet batch,
produces pass/veto verdicts, and calls `picker-finalize-pending`. Deterministic
validation rejects any critic model ID that is not Grok or equals the analyst.

## Execution

Robinhood MCP required. Requires `AGENTIC_TRADER_ACCOUNT`,
`AGENTIC_TRADER_NET_DEPOSITS`, and `DATABASE_URL`. Flow:

1. Fresh equity quant plus option positions, orders, chains, instruments, and
   quotes from the broker
2. Consume only a batch finalized by the independent Grok critic
3. `picker-authorize-batch` and `option-authorize-batch`
4. `picker-plan` → `live-plan`; `option-plan` for exact Level 2 limit orders
5. In `MODE: LIVE`, review and place only approved equity/option orders
6. Reconcile both asset classes; sync either lifecycle only when both are clean

Change only the first `MODE:` line between `PLAN_ONLY` and `LIVE`. Options use
their committed initial caps; do not create a temporary cap-bypass automation.
