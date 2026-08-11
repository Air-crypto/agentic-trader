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
| AI Picker Research | `0 12 * * 1-5`; `15 15,17 * * 1-5` | 08:00/11:15/13:15 EDT; 07:00/10:15/12:15 EST | `automations/research-prompt.txt` |
| Independent Grok Critic | `0 14 * * 1-5`; `15 16,18 * * 1-5` | 10:00/12:15/14:15 EDT; 09:00/11:15/13:15 EST | `automations/critic-prompt.txt` |
| AI Picker Live Session | `0 15,17,19 * * 1-5` | 11:00/13:00/15:00 EDT; 10:00/12:00/14:00 EST | `automations/execution-prompt.txt` |

All three Live UTC hours are inside regular trading hours year-round. Each
midday Research run starts 15 minutes after the preceding Live run, so a newer
pending batch cannot accidentally block that cycle's execution.

Wire JSON for Cursor is in `automations/research.json`,
`automations/critic.json`, and `automations/execution.json`. Root
`automation.json` / `automation-prompt.txt` mirror execution. Repo JSON does not
push schedules into the product UI by itself.

Disable the old static SPY/IEF/GLD automation in the Cursor UI so two live
sessions do not compete for the shared daily $800 / 8-order budget.

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

Across all three Live runs, broker and durable counters enforce one shared
`$800 / 8-order` total. New entries may consume at most `$600 / 6 orders`; the
remaining `$200 / 2 orders` is reserved for risk-reducing exits.
