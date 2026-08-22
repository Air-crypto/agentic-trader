# Option Execution

## Current policy: no new options

The live-canary workflow is equity-only. The morning and evening tasks do not create, authorize, plan, review, reserve, or place a new option order. An equity thesis may not be converted into calls, puts, spreads, covered calls, or any other option structure.

This policy is independent of confidence, premium size, buying power, or user consent to equity trading. A general instruction to trade does not authorize an option.

## Existing option positions

Existing and manually opened option positions are preserved by default. The scheduled tasks may read them for account reconciliation and report expiration, assignment, exercise, collateral, concentration, or liquidity risk. They must not close, cancel, roll, exercise, or otherwise modify an option automatically.

A separately requested, risk-reducing close is outside the scheduled equity canary. Before any such close, the workflow must identify the exact account, underlying, OCC contract, side, quantity, limit, session, and expiry; validate the current broker state and quote; produce a fresh deterministic plan and broker review; and obtain explicit confirmation of that exact close order. No close request authorizes opening a replacement contract.

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

No option mutation may rely on a schedule, prior approval, live mode, or general consent. Any separately authorized risk-reducing close requires exact per-order confirmation after review, and its plan expires after five minutes. A changed quote, contract, quantity, session, or account state requires a new review and confirmation.

The audit trail should append the observed position, risk warning, proposal, review, user confirmation, reservation, broker request, and reconciliation. Counterfactual-learning and knowledge-graph writes are nonblocking telemetry and do not authorize or prohibit an order.
