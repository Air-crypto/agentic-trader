# AI Stock Picker

## Role in the live canary

The picker produces point-in-time equity research and a ranked candidate set for the twice-daily live-canary workflow. It does not control sizing, approve risk, or place orders. Deterministic portfolio and execution code remains authoritative, and every concrete Robinhood order requires exact user confirmation.

The picker has not demonstrated guaranteed edge. Negative account performance should be treated as evidence for tight exposure, careful measurement, and the option to make no trade.

## Candidate and evidence rules

For every eligible candidate, including rejects, the research packet records the available factors, evidence timestamps, source identities, and rejection reasons. Candidate evaluation must use only information available at the decision time.

- Social sources are discovery and sentiment inputs only.
- An actionable thesis requires a current primary source from a registered issuer or exchange.
- SEC ingestion is disabled in the current scheduled deployment.
- If primary evidence is missing, stale, contradictory, or unavailable, reject the candidate.
- The do-nothing result is always valid and should win when evidence or expected value is weak.

A separate critic reviews the analyst's evidence and reasoning. The critic is independent of the analyst role and is not tied to any single model vendor.

## Twice-daily behavior

### Morning

The morning task researches and ranks candidates for regular-hours execution. Surviving proposals pass through deterministic allocation and risk controls, a five-minute plan, and Robinhood review.

### Evening

The evening task applies the same evidence and risk process, then may surface at most one opening proposal. It must be a whole-share GFD limit order, no more than `$100`, explicitly eligible for all-day hours, and based on a fresh quote with a spread no wider than `10 bps`.

The picker may not convert an equity idea into an option order.

## Deterministic portfolio envelope

Picker scores cannot override these live-canary ceilings:

- at most `3` concurrent names;
- at most `3.5%` of equity in one name;
- at most `7%` of equity in one sector;
- at least `89.5%` cash;
- at most `2` new entries and `$300` aggregate new-entry notional per day;
- stop new entries at a `0.5%` daily loss or `3%` drawdown.

Current and manually opened holdings are preserved unless the user explicitly authorizes a specific close. The picker can recommend a close, but that recommendation has no execution authority.

## Execution boundary

After deterministic authorization, the workflow creates a plan that expires in five minutes and requests a nonplacing Robinhood order review. It must then show the exact symbol, side, shares, limit, session, time in force, notional, and expiry.

The scheduled run stops there. Reservation and placement are permitted only after the user confirms that exact order. If the plan expires or any material account, quote, or order field changes, the workflow must rebuild, re-review, and ask again.

## Counterfactual learning

The learning subsystem freezes predictions before outcomes and retains the full candidate set, including rejects, so later evaluation can estimate selection effects. When data is available it records forward returns over `1`, `3`, `5`, `20`, and `60` trading days versus SPY and sector benchmarks, together with model, prompt, feature, and data-snapshot hashes.

Evaluation should remain point-in-time and cluster or block uncertainty by decision date. Factor-only, LLM-only, hybrid, and do-nothing results can be compared using coverage, turnover and cost, drawdown, information coefficient, and uncertainty.

These records are diagnostic telemetry, not an execution dependency or promotion gate. A telemetry or knowledge-graph failure must not loosen a risk limit, change a deterministic decision, or block an otherwise valid user review. It should be logged and repaired separately.

## Knowledge graph

The graph may link candidates, issuers, sectors, evidence, theses, critics, decisions, plans, fills, and later outcomes. It supports traceability and counterfactual analysis. It is not an authorization engine and may never supply missing primary evidence on its own.
