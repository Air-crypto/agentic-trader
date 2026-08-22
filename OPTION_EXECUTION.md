# Option Execution

## Current policy: no scheduled option workflow

The live-canary workflow is equity-only. The morning and evening tasks do not create option drafts or authorize, plan, review, reserve, place, close, cancel, roll, exercise, or otherwise mutate an option. An equity thesis may not be converted into calls, puts, spreads, covered calls, or any other option structure.

This policy is independent of confidence, premium size, buying power, or user consent to equity trading. A general instruction to trade does not authorize an option.

## Existing option positions

Existing and manually opened option positions and orders are read-only broker-truth inputs. The scheduled tasks may read them for account reconciliation and report expiration, assignment, exercise, collateral, concentration, or liquidity risk. They must not turn those inputs into an option draft, authorization, plan, review, or mutation.

The repository retains tested exact-batch close-only authorization, planning, and reservation primitives, but no end-to-end option-close workflow is activated or supported by the scheduled equity tasks. A separately user-triggered workflow would still need to identify the exact account, underlying, OCC contract, side, quantity, limit, session, and expiry; validate current broker state and quote; produce a fresh broker review; and obtain explicit confirmation of that exact close order. Until that separate workflow is activated, these tasks must not invoke the close-only primitives or attempt an option close. No close request can authorize opening a replacement contract.

## Prohibited scheduled actions

- opening any long or short option position;
- adding to, rolling, or replacing an existing contract;
- multi-leg orders;
- uncovered calls or puts;
- zero-DTE entries;
- automatic exercise or do-not-exercise instructions;
- relying on a model's maximum-loss calculation instead of broker-confirmed contract and collateral state;
- using option buying power to bypass the equity canary's cash reserve or loss limits.

## Monitoring requirements

If an option is present in the account, reconciliation should report at least:

- full OCC contract identity and multiplier;
- long or short quantity and pending orders;
- expiration and days to expiry;
- current mark, spread, and quote age when available;
- exercise or assignment exposure;
- broker-reported collateral and buying-power impact;
- any mismatch between local records and Robinhood.

Missing or inconsistent option state is a reason to block new equity entries until the account is understood. It is not permission to mutate the option position.

## Confirmation and audit

No option mutation may rely on a schedule, prior approval, live mode, or general consent. Exact per-order confirmation is necessary but not sufficient for any separately activated close-only workflow. Its plan must expire after five minutes, and a changed quote, contract, quantity, session, or account state requires a new review and confirmation.

For scheduled runs, the audit trail records only the observed option position/order and risk warning. Any separately activated close-only workflow must also append its proposal, review, user confirmation, reservation, broker request, and reconciliation. Counterfactual-learning and knowledge-graph writes are nonblocking telemetry and do not authorize or prohibit an order.
