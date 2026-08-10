# Agentic Trader

A reproducible research and paper-trading harness for a conservative, monthly
momentum strategy.

## Real-money execution

One small account is enabled for live trading under
[`REAL_MONEY_EXECUTION.md`](REAL_MONEY_EXECUTION.md). The account is identified
by the `AGENTIC_TRADER_ACCOUNT` environment variable and is never committed;
with it unset the guard rejects every order. Orders must pass the
deterministic guard in `src/agentic_trader/execution.py`, which enforces an
account allowlist, a symbol allowlist, per-order and per-day notional caps,
concentration limits, daily-loss and drawdown halts, and a `KILL_SWITCH` file.
The guard has no network access and cannot place an order; it only approves or
rejects. Live trading now uses the Stage 3 AI stock picker under
[`AI_STOCK_PICKER.md`](AI_STOCK_PICKER.md): research stages evidence into
Postgres; execution authorizes picks and places only guard-approved orders.
The picker is unvalidated — same order caps and reconciliation still apply.
Bounded Level 2 options are governed separately by
[`OPTION_EXECUTION.md`](OPTION_EXECUTION.md). Only guard-approved long calls,
long puts, covered calls, cash-secured puts, and closes are eligible; naked
options, multi-leg spreads, 0DTE, and market option orders remain blocked.

```bash
uv run agentic-trader picker-authorize-batch --quant artifacts/picker/quant.json
uv run agentic-trader picker-plan --snapshot artifacts/picker/snapshot.json
uv run agentic-trader live-plan --request artifacts/live/request.json --record-equity
uv run agentic-trader option-migrate
uv run agentic-trader option-authorize-batch --snapshot artifacts/options/snapshot.json
uv run agentic-trader option-plan --snapshot artifacts/options/snapshot.json
touch KILL_SWITCH   # halts all order approval immediately
```

## Current result

Two cost-aware out-of-sample studies were run from 2015 through the latest
available market day:

- The ETF-only version **failed** the research gates. It produced a 5.48% CAGR,
  0.45 Sharpe versus cash, and a -17.71% maximum drawdown.
- The ETF plus fixed large-cap version passed the numeric gates with a 6.29%
  CAGR, 0.62 Sharpe versus cash, and a -10.94% maximum drawdown.
- The large-cap result is **not deployment evidence** because using today's
  successful large-cap names in historical years creates survivorship bias.
- SPY buy-and-hold returned much more over this unusually strong equity sample,
  but its maximum drawdown was -33.72%.

The strategy is therefore a paper-trading candidate, not a confirmed money
maker. Historical performance cannot establish future profitability.

## Architecture tournament

The project now compares pure algorithms, LLM-only research, and a gated hybrid
under `STRATEGY_TOURNAMENT.md`.

Five ETF algorithms were selected on 2010–2018 data and evaluated without
reselection on 2019–2026. The development winner was a fixed
50% SPY / 25% IEF / 15% GLD / 10% BIL portfolio. Its holdout CAGR was 10.62%
with 0.85 Sharpe versus BIL, but its -17.86% maximum drawdown failed the
mandate. No pure-algorithm candidate is promoted.

The diversified absolute-trend challenger had a 7.82% holdout CAGR, 0.69
Sharpe, and -10.01% maximum drawdown. It remains forward-paper-only because
selecting it after observing holdout results would be data snooping.

```bash
uv run agentic-trader tournament --output artifacts/tournament
uv run agentic-trader tournament-signal \
  --output artifacts/tournament/shadow-targets.json
```

LLM-only trading remains unscored until there is a frozen, point-in-time news
corpus and reproducible model output. The current hybrid therefore uses an LLM
only to extract structured evidence; its event-driven portfolio weight is zero.

## Broad instrument research

`UNIVERSAL_MARKET_RESEARCH_AGENT.md` permits the research model to investigate
any stock, ETF, leveraged/inverse ETF, or listed option structure. It can choose
the thesis, direction, horizon, and expression. Deterministic code still checks
the evidence, data sufficiency, option payoff, bounded loss, and paper sizing.

```bash
uv run agentic-trader analyze --tickers AAPL,NVDA,TQQQ,UPRO,TMF,IBIT
uv run agentic-trader option-chain --symbol SPY
uv run agentic-trader option-analyze \
  --spec research/examples/defined-risk-call-spread.json
uv run agentic-trader proposal-validate \
  --proposal path/to/proposal.json \
  --evidence-bundle path/to/evidence-bundle.json
```

Options are not included in historical performance claims because the current
data source has no point-in-time option chains. The option engine calculates
expiration payoff, breakevens, bounded or unbounded loss, Black-Scholes
approximations, and aggregate Greeks. A historical options provider must be
connected before an options strategy can enter the tournament.

## Model trial

`MODEL_EVALUATION.md` records a frozen Sonnet 5 versus Grok 4.5 research trial.
Sonnet scored 94/100 and Grok scored 84/100. Both rejected their candidate
theses; Sonnet won by using more of the auditable toolchain and discovering a
recent-event data-window defect that has since been fixed.

## Strategy

At each month-end, using adjusted data available at that close:

1. Calculate six-minus-one-month and twelve-minus-one-month momentum.
2. Require risk assets to be above their 200-day moving average and to beat BIL
   on twelve-minus-one-month momentum.
3. Rank eligible assets by the average momentum score and select up to four.
4. Fill unused slots with eligible IEF, TLT, or GLD; hold residual capital in
   BIL.
5. Size selected assets by inverse 63-day volatility, cap any asset at 35%,
   cap a stock at 15%, and cap the total stock sleeve at 30%.
6. Scale risk down to an 8% annualized volatility target without leverage.
7. Apply the signal on the following trading day.

The simulator charges 10 basis points on every bought or sold dollar. A soft
drawdown rule halves non-cash exposure at -8%; a -10% control drawdown moves the
paper portfolio to BIL for 21 trading days. That control rule targets risk but
cannot guarantee a 10% cumulative maximum drawdown.

## Run

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run agentic-trader validate --output artifacts/etf-core
uv run agentic-trader validate --include-stocks --output artifacts/etf-large-cap
uv run agentic-trader signal --capital 5000 --output artifacts/latest/paper-signal.json
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

`validate` exits with status 2 when a research gate fails. Generated artifacts
include daily equity, realized weights, point-in-time decisions, rolling
two-year windows, a 27-run parameter-neighborhood check, and `summary.json`.

`signal` writes target paper holdings. It deliberately produces no executable
broker order.

## Alternative-data research

`ALTERNATIVE_DATA_AGENT.md` defines a browser-enabled research loop for mapping
news and primary disclosures through private customers to public suppliers. It
requires a verified relationship, company-level materiality, a publication
timestamp, novelty, and a cost-aware event study before a hypothesis can affect
even the shadow paper portfolio.

The seeded commercial-space/industrial-gas example currently rejects the simple
“more launches, buy the oxygen supplier” thesis as unproven. Four Linde events
from 2020–2025 produced mean SPY-relative returns after costs of -1.13% after
one day, +0.38% after 20 days, and -3.01% after 60 days. The 20-day t-statistic
was 0.18. Four events from one company are far below the required 30 events and
five tickers.

```bash
uv run agentic-trader event-score \
  --bundle research/seeds/commercial-space-industrial-gases.json
uv run agentic-trader event-study \
  --bundle research/seeds/commercial-space-industrial-gases.json
```

News search is discovery only. Eligible evidence comes from timestamped SEC
filings, government records, issuer/customer releases, and independently
verified reporting. The event-study engine enters on the next trading day,
compares 1/5/20/60-day performance with SPY, and subtracts 20 basis points of
round-trip costs.

## Research gates

The candidate must:

- earn a positive out-of-sample CAGR and beat BIL;
- have Sharpe versus BIL of at least 0.50;
- keep observed out-of-sample maximum drawdown within 12%;
- have positive returns in at least 70% of rolling two-year windows;
- have positive CAGR in at least 80% of nearby parameter runs; and
- have median nearby-parameter Sharpe of at least 0.40.

A numeric pass is necessary but not sufficient. Before any manual live trade,
replace the stock study with point-in-time constituent data and complete at
least 60 market days of forward paper trading with executable bid/ask quotes.

## Data and limitations

Yahoo adjusted closes are cached under `.cache/market-data`. They are convenient
for research but are not execution-quality data. The study does not model
taxes, partial fills, rejected orders, opening gaps, or exact fractional-share
eligibility. Options, leverage, margin, and shorting are intentionally excluded:
they conflict with the selected small-account and approximately 10% drawdown
mandate.

See `PAPER_TRADING_AGENT.md` for the model-agnostic operating procedure.
