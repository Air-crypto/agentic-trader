# Cursor Automation setup

Create two **Private**, repository-backed Cursor Automations on
`Air-crypto/agentic-trader`, branch `main`, using the successful Cloud Agent
Build from `.cursor/environment.json`.

For both automations:

- select `gpt-5.6-sol`, the only model used for research and draft generation;
- do not configure a critic, subagent, or self-critique step, and never fabricate
  a critic verdict;
- recognize that deterministic guards do not detect semantic evidence conflicts
  or judge catalyst freshness; require the sole analyst's counter-thesis,
  invalidation, and identified contradictions in the report for human scrutiny;
- remove the **Open Pull Request** tool;
- disable Cursor Memories if the UI permits it; and
- attach a private Robinhood Cloud MCP connection scoped to read and
  `review_equity_order` only. Do not attach placement/cancel/replace/exercise
  tools to a scheduled task when tool-level scoping is available.

## Runtime Secrets and local confirmation key

Add these environment-scoped **Runtime Secrets** in Cursor. Never paste their
values into Agent Instructions, chat, logs, or the repository:

- `DATABASE_URL`
- `AGENTIC_TRADER_NET_DEPOSITS`
- `AGENTIC_TRADER_CONFIRMATION_PUBLIC_KEY`

`AGENTIC_TRADER_ACCOUNT` is unnecessary when get_accounts exposes exactly one
agent-accessible intended account. `SEC_USER_AGENT` is unnecessary because SEC
ingestion is disabled.

Create the confirmation key once on the user's Mac, outside the repository:

```bash
uv run agentic-trader confirmation-keygen \
  --private-key ~/.config/agentic-trader/confirmation-ed25519.pem
chmod 600 ~/.config/agentic-trader/confirmation-ed25519.pem
```

Put only the printed public value in the matching Cursor Runtime Secret. The
private PEM never enters Cursor, Supabase, GitHub, a prompt, or a cloud VM. When
an exact review qualifies, run its displayed `confirmation-sign` command on the
Mac. Send the signed one-line result only to the separate user-triggered
executor, never back to either scheduled automation.

## Required Robinhood reads

A manual cloud validation must prove these installed schemas and pagination:

1. `get_accounts()` and the sole `agentic_allowed=true` account;
2. `get_portfolio(account_number)` including `total_value`, `cash`, the complete
   `buying_power` object with `unleveraged_buying_power`, and
   `pending_deposits`;
3. every page of `get_equity_positions(account_number)`;
4. every page of `get_option_positions(account_number, nonzero=true)`;
5. every relevant page of `get_equity_orders(account_number)` and
   `get_option_orders(account_number)`; and
6. `review_equity_order` returning `order_checks={}` (any nonempty broker alert
   blocks the order), nonempty `quote_data`, and nonempty
   `market_data_disclosure`.

Option positions and orders are read-only broker-truth inputs for reconciliation
and equity risk checks. The scheduled tasks must not create option drafts or
authorize, plan, review, or mutate an option. Tested exact-batch close-only CLI
primitives remain in the repository, but no option-close workflow is activated
or supported by these tasks.

Preserve the broker's native order `state` and `id`. Order-list rows do not
expose the placement `ref_id`, so an existing attempt is reconciled by its
persisted broker order ID with `get_equity_orders(order_id=<id>)`. Never infer a
ref_id or fuzzy-match an unknown submission. An ambiguous attempt without a
broker ID remains unknown and keeps the durable all-order halt engaged.

The currently installed Robinhood MCP exposes no advanced/OCO-order read. Both
prompts therefore require
`broker_advanced_orders_complete_for_session=false`, which deliberately blocks
all new entries. Never fabricate completeness. Research and deterministically
approved risk-reducing equity exits may continue.

The caller must pass native `account.type` (`cash` or `limited_margin` for this
no-leverage policy), portfolio `cash`, the complete native buying-power object,
and pending deposits. Repository code derives spendable unleveraged cash. Do
not supply invented `cash_without_margin`, `margin_not_used`, `no_margin`, or
leveraged-classification fields.

## Morning automation

Name: `Agentic Trader - Morning Live Research & Review`

Cursor's displayed preview currently shows UTC cron interpretation. During
Pacific daylight time use:

```text
35 13 * * 1-5
```

The preview must say **6:35 AM Pacific, Monday-Friday**. During Pacific standard
time use `35 14 * * 1-5`. If Cursor exposes a genuine
`America/Los_Angeles` timezone selector, use it with local cron
`35 6 * * 1-5`. Trust the next-run preview.

Paste into **Agent Instructions**:

```text
Run the production morning research-and-review workflow in this repository.
Read automations/morning-live-prompt.txt completely and follow it as the
canonical contract together with every document it names. This scheduled task
is nonplacing: never confirm, reserve, claim, place, cancel, replace, exercise,
or otherwise mutate an order, even if a signed reply appears. Treat the checkout
as ephemeral, Supabase and Robinhood as authority, and fail closed if the
Runtime Secrets, schema, lease, configured `gpt-5.6-sol` model, complete native
broker truth, or Robinhood Cloud MCP is unavailable. Use no critic or
self-critic; pass analyst drafts directly through deterministic validation. Do
not edit code, commit, push, open a pull request, revive the naive trader prompt,
or bypass the local signed exact-review handoff.
```

## Evening automation

Name: `Agentic Trader - Evening Live Research & Review`

During Pacific daylight time use:

```text
15 1 * * 1-5
```

The preview must say **6:15 PM Pacific, Sunday-Thursday**. During Pacific
standard time use `15 2 * * 1-5`. With a genuine
`America/Los_Angeles` selector, use local cron `15 18 * * 0-4` instead.

Paste into **Agent Instructions**:

```text
Run the production evening research, measurement, KG, and review workflow in
this repository. Read automations/evening-live-prompt.txt completely and follow
it as the canonical contract together with every document it names. This
scheduled task is nonplacing: never confirm, reserve, claim, place, cancel,
replace, exercise, or otherwise mutate an order, even if a signed reply appears.
Treat the checkout as ephemeral, persist runtime state in Supabase, and fail
closed if the Runtime Secrets, schema, lease, configured `gpt-5.6-sol` model,
complete native broker truth, or Robinhood Cloud MCP is unavailable. Use no
critic or self-critic; pass analyst drafts directly through deterministic
validation. Never edit code or repository KG Markdown, commit, push, open a
pull request, revive the naive trader prompt, or bypass the local signed
exact-review handoff.
```

## Required single-model batch sequence

Each scheduled run must create a fresh cycle ID and batch ID, preserve them
through deterministic authorization, and use this exact sequence:

```bash
uv run agentic-trader picker-cycle-start --cycle-id <cycle_id> --as-of YYYY-MM-DD
uv run agentic-trader picker-stage --bundle <bundle.json>
uv run agentic-trader picker-authorize-batch --batch-id <batch_id> --quant <quant.json> --as-of YYYY-MM-DD --output <authorized-batch.json>
uv run agentic-trader picker-plan --batch-id <batch_id> --packets <authorized-batch.json> --snapshot <broker-snapshot.json> --as-of YYYY-MM-DD --output <request.json>
```

Create exactly one immutable bundle for the cycle. It must contain
`model_id: "gpt-5.6-sol"`, `review_mode: "single_model_direct"`, the exact
`cycle_id`, one new `batch_id`, and internally consistent hashes, evidence, and
equity drafts, with no critic fields or option drafts. Keep those identifiers and
contents unchanged.
`picker-stage` directly stages the bundle and finalizes the cycle. Authorization
must name that exact new `batch_id`; never select a prior or "latest" same-day
batch. The authorizer output includes a `packets` list. Pass that exact file and
the same `batch_id` to `picker-plan`. Never omit `--batch-id` or `--packets` from
`picker-plan`. The CLI intentionally has no same-day database packet fallback;
missing exact packet input must fail rather than replay an older packet.

Never run the retired `picker-stage-pending`, `picker-export-pending`, or
`picker-finalize-pending` commands.

## One-time deployment

Run migrations only as a reviewed deployment action, never in a schedule:

```bash
uv run agentic-trader cloud-migrate
uv run agentic-trader cloud-schema-check
```

The current `picker-record-close` implementation intentionally returns
`official_close_equity_source_unavailable`; it cannot write the required
verified account-equity anchor. Until a broker-verified close-time collector is
implemented and the durable anchor is current, all new entries must remain
fail-closed. This does not disable research or deterministically authorized
exits.

Use the checked-out code's `execution.DEFAULT_SYMBOL_ALLOWLIST` as the maximum
live buy universe. Caller data may narrow but never expand it. Pass only the
code-owned sector taxonomy (`source=agentic_trader_code_owned`,
`version=agentic-gics-v1`) for all holdings/planned symbols. Do not infer sectors
or claim that Robinhood scanner `instrument_type=EQUITY` proves stock/ETF or
unleveraged status; missing accepted classification blocks an entry.

## Manual validation before activation

Keep both toggles inactive until a fresh cloud VM proves:

1. the run diagnostics show `gpt-5.6-sol` as the sole model, with no critic or
   self-critic invocation;
2. one immutable single-model bundle directly stages and finalizes its fresh
   cycle, and authorization rejects an omitted, mismatched, prior, or replayed
   batch ID;
3. option positions and orders remain read-only broker-truth inputs, and the
   scheduled trace contains no option draft, authorization, plan, review, or
   mutation;
4. frozen install, tests, Ruff, graph check, and `cloud-schema-check` pass;
5. acquire/heartbeat/finish of a Supabase schedule-window lease works;
6. all required Robinhood read pages succeed and reports mask account IDs;
7. startup recovery uses native states and broker IDs, and an unknown attempt
   without a broker ID remains halted rather than being guessed or retried;
8. absence of advanced/OCO truth and the verified close anchor blocks every new
   entry while research and eligible equity exits remain available;
9. the code-owned buy universe and versioned sector taxonomy cannot be expanded
   by caller/model data, and missing classification rejects entry;
10. review persistence contains unchanged planned broker parameters, exact
   `order_checks`, full `quote_data`, verbatim `market_data_disclosure`, complete
   native response, and Robinhood provenance;
11. a fresh VM can export the same durable plan/review, and any broker-authority
    fingerprint, parameter, taxonomy, quote, account, position, order, session,
    hash, or expiry change voids confirmation;
12. local Ed25519 signing accepts only the exact unexpired review hash; and
13. the scheduled run's tool trace contains no live-confirm, live-reserve,
    submission claim, place, cancel, replace, exercise, repository edit, or PR.

After those checks, the schedules can run as research/review-only jobs. Expect
new entries to remain rejected until both the advanced/OCO truth source and
verified official-close collector exist.

## Broker tool boundary

Cursor attaches every tool exposed by an MCP server. Repository signatures and
Supabase claims secure the supported executor path, but cannot physically stop
an agent from directly calling an attached broker write tool. A hard boundary
requires a read/review-only Robinhood connection for schedules and a separate
user-triggered executor/proxy for placement. The executor must fetch and
paginate Robinhood truth itself; JSON supplied by the scheduled agent cannot
prove broker-history completeness. This repository does not deploy that proxy.
If Cursor cannot scope the MCP, run the schedules as research/review-only and
do not enable write execution unless the user explicitly accepts that the
no-placement boundary is prompt-enforced rather than permission-enforced.
