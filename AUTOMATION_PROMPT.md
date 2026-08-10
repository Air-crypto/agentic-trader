# Automation Operating Prompts

Stage 3 uses two Cursor Automations. Keep these files in sync with
`AI_STOCK_PICKER.md` and `REAL_MONEY_EXECUTION.md`. Where they disagree, the
contracts win and the run should stop and say so.

| Automation | Schedule (UTC) | Local intent | Prompt source |
| --- | --- | --- | --- |
| AI Picker Research | `0 12 * * 1-5` | Weekdays 5:00 AM PDT / 8:00 AM EDT | `automations/research-prompt.txt` |
| AI Picker Live Session | `0 16 * * 1-5` | Weekdays 9:00 AM PDT / 12:00 PM EDT | `automations/execution-prompt.txt` |

Wire JSON for Cursor is in `automations/research.json` and
`automations/execution.json`. Root `automation.json` / `automation-prompt.txt`
mirror the execution automation.

Disable the old static SPY/IEF/GLD automation in the Cursor UI so two live
sessions do not compete for the daily $400 / 4-order budget.

## Research

No Robinhood MCP. Requires `DATABASE_URL` set to the Supabase Shared Pooler URI
from the Connect panel (`*.pooler.supabase.com`, user `postgres.<project-ref>`).
Direct `db.*.supabase.co` hosts fail with `Network is unreachable` (IPv6-only);
a pooler host with user `postgres` fails password auth. Stages verified
evidence, drafts, and critic verdicts via `picker-stage`. Never places orders.

## Execution

Robinhood MCP required. Requires `AGENTIC_TRADER_ACCOUNT`,
`AGENTIC_TRADER_NET_DEPOSITS`, and `DATABASE_URL`. Flow:

1. Fresh quant snapshots from the broker
2. `picker-authorize-batch`
3. `picker-plan` → `live-plan`
4. In `MODE: LIVE`, place approved orders, reconcile, then `picker-sync`

Change only the first `MODE:` line between `PLAN_ONLY` and `LIVE`.
