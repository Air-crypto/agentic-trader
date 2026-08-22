# Model Research Evaluation

Two models received the same frozen broad-market research task, repository
contracts, tool access, and paper-only constraints on August 9, 2026.

This is a historical benchmark record, not the current deployment contract.
The current Cursor deployment uses only `gpt-5.6-sol` for research and draft
generation, with no independent critic or self-critic. Analyst drafts pass
directly through deterministic validation, and no critic verdict is fabricated.
That validation does not detect semantic evidence conflicts or judge catalyst
freshness; counter-thesis, invalidation, and identified contradictions remain
single-analyst fields for human scrutiny, not an equivalent independent review.

## Result

Claude Sonnet 5 scored **94/100**. Grok 4.5 scored **84/100**.

Both reached the correct high-level decision: reject the selected thesis rather
than force a position through weak novelty or materiality evidence. Sonnet won
because it exercised substantially more of the machine-verifiable workflow and
found a real defect in the recent-event study command.

## Frozen rubric

| Category | Weight | Sonnet 5 | Grok 4.5 |
|---|---:|---:|---:|
| Primary-source quality and fact/inference separation | 25 | 24 | 22 |
| Causal chain and company-level materiality | 15 | 14 | 13 |
| Quantitative tool use and reproducibility | 15 | 15 | 9 |
| Options/leverage understanding | 10 | 8 | 8 |
| Falsifiability, counter-thesis, and risk | 15 | 14 | 14 |
| Schema and evidence auditability | 10 | 9 | 8 |
| Calibration and willingness to reject | 10 | 10 | 10 |
| **Total** | **100** | **94** | **84** |

## Sonnet 5

Sonnet screened 12 instruments and selected Centrus Energy as a hypothesis to
interrogate rather than a momentum recommendation. It assembled a
machine-readable evidence bundle using DOE, SEC, and issuer sources; ran event
scoring; attempted the event study; ran proposal validation; independently
checked price reaction; and explicitly overrode a mechanical risk pass because
the economic evidence failed novelty and already-priced tests.

Most importantly, it found that `event-study` fetched only 30 pre-event calendar
days while the data layer required 253 observations. This made the command fail
for recent events. The command now fetches 400 pre-event days, and the Sonnet
bundle runs successfully. Its corrected study remains rejected: two measurable
events, one ticker, one year, -9.26% mean 20-day abnormal return, and t-statistic
-0.65.

Weaknesses:

- The executive decision was reject/effective weight zero, while the returned
  proposal retained a hypothetical 1% target weight. The explanation was clear,
  but a strict schema should encode zero in a rejected proposal.
- It used a direct Python call to inspect daily prices because no dedicated
  event-window CLI existed.
- Some evidence remained issuer-sourced and needs independent appropriations
  data.

## Grok 4.5

Grok screened 10 instruments across power, industrials, uranium, energy,
healthcare, and leveraged semiconductors. It correctly refused to treat SOXL's
large descriptive score as a forecast and rejected both a Constellation nuclear
restart thesis and an AEP Texas financing thesis for weak incremental novelty
or shareholder materiality. Its NRC, SEC, customer, issuer, and DOE evidence was
generally strong, and its counter-thesis and monitoring plan were disciplined.

Weaknesses:

- It stopped after the quantitative screen and browser research. It did not run
  event scoring, event study, proposal validation, or option analysis.
- Its proposal added fields outside the documented schema and used evidence IDs
  that were not demonstrated through a validated bundle.
- It surfaced a date typo while correcting itself and did not quantitatively
  screen AEP before discussing it.

## Tool follow-up

Implemented after the trial:

1. Recent-event studies now fetch enough pre-event history to satisfy the data
   layer.
2. `option-chain` now saves a timestamped current Yahoo chain with bid, ask,
   midpoint, spread, implied volatility, volume, open interest, intrinsic value,
   extrinsic value, and last-trade age.
3. A live SPY snapshot test returned 278 contracts for the August 10, 2026
   expiration, including 152 contracts with positive bids and open interest.

Still missing:

- point-in-time historical option chains;
- structured SEC, NRC, FERC, congressional, and USAspending monitors;
- execution-quality NBBO and borrow data; and
- a sufficiently broad labeled event corpus for economic validation.

## Current operating choice

Use `gpt-5.6-sol` as the single Cursor model. Do not configure Sonnet, Grok, or
another model as a critic or challenger, and do not substitute self-critique for
independence. Rerun a frozen benchmark after material prompt, tool, model, or
data changes, but keep benchmark comparison separate from the deployed
authorization path.
