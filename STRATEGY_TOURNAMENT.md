# Strategy Tournament Protocol

The choice between algorithms, language models, and a hybrid is an empirical
question. No architecture receives capital because it sounds more intelligent.

## Current conclusion

The best supported architecture today is:

```text
deterministic portfolio and risk engine
        +
LLM/browser research as a structured evidence extractor
        +
event satellite fixed at zero until its own tests pass
```

This is not a claim that the hybrid has an edge. It is a separation of duties:
deterministic code controls sizing and reproducibility; an LLM can search and
normalize messy evidence; no LLM output affects even shadow sizing until a
historical and forward test passes.

## Tested pure-algorithm families

Five ETF strategies were preregistered before viewing the 2019+ holdout:

- a fixed 50% SPY / 25% IEF / 15% GLD / 10% BIL allocation;
- volatility-targeted SPY absolute trend;
- diversified absolute trend across liquid global, real-asset, and bond ETFs;
- cross-sectional relative momentum; and
- an equal-weight ensemble of the three dynamic strategies.

The development period was 2010–2018. A strategy qualified there by beating
BIL, having Sharpe versus BIL of at least 0.40, and limiting maximum drawdown to
15%. The highest development Sharpe then became the sole selected strategy for
the 2019+ holdout.

The fixed balanced strategy won development with 7.68% CAGR, 0.91 Sharpe, and
-10.15% maximum drawdown. In the holdout it produced 10.62% CAGR and 0.85
Sharpe, but its maximum drawdown reached -17.86%. It therefore failed the
approximately 10% drawdown mandate.

The diversified absolute-trend challenger happened to produce 7.82% CAGR, 0.69
Sharpe, and -10.01% maximum drawdown in the holdout. It cannot be substituted
after seeing that result. It remains a future challenger whose rules must stay
frozen for forward paper observation.

## LLM-only arm

An LLM-only trading strategy is not currently testable because the project does
not have a sufficiently broad, point-in-time historical news corpus with frozen
model outputs. Re-running a current model over selected old articles would
introduce model-version, retrieval, survivor, and hindsight leakage.

Before an LLM-only arm can enter the economic tournament, freeze:

- exact model identifier and inference settings;
- system prompt and evidence schema;
- retrieval query and source allowlist;
- article body, publication timestamp, and retrieval timestamp;
- raw model response before any human correction; and
- deterministic conversion from response to a signed paper weight.

First test extraction quality on a human-labeled set. Required minimums are 80%
precision on relationships, 70% recall, zero future-timestamp evidence, and
zero invented customer/supplier links. Then apply the same event-study gates as
`ALTERNATIVE_DATA_AGENT.md`.

## Hybrid arm

The hybrid combines a frozen pure-algorithm core with a capped event satellite.
It may enter a paper comparison only after:

- the event corpus contains at least 30 eligible events and five tickers;
- the 20-day event study exceeds 0.5% after costs with t-statistic at least 2;
- no ticker contributes more than 25% of events;
- placebo tests using shuffled dates and tickers do not reproduce the result;
- extraction-quality gates pass; and
- the complete portfolio improves holdout or forward Sharpe without worsening
  maximum drawdown beyond 12%.

The initial commercial-space/industrial-gas sample fails these gates, so the
hybrid event weight is exactly zero.

## Testing discipline

Every new hypothesis is written down before its result is viewed. Record:

- strategy rule and parameters;
- economic rationale;
- universe and point-in-time membership;
- development, validation, and holdout boundaries;
- costs, lag, and missing-data policy;
- primary metric and rejection gates; and
- every attempted variant, including failures.

Do not weaken a gate, move a date boundary, add a winning asset, or change a
lookback after viewing holdout results. A changed strategy receives a new name
and must wait for new forward data.

## Next experiment

Run three frozen shadow books from the next market day:

1. development-selected fixed balanced strategy;
2. diversified absolute trend challenger;
3. hybrid book equal to the diversified trend challenger plus a zero-weight
   event sleeve until the event gates pass.

The pure and hybrid challengers will initially hold identical positions. This
is intentional: the first valid comparison begins only when a prequalified
event enters the hybrid book. Review after at least 60 market days and continue
until there are enough independent event observations for inference.
