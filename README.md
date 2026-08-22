# Overnight Return Prediction — Research Log

A negative result, documented. Tests whether the intraday-to-overnight reversal
effect in liquid US large caps can be traded, using two independent datasets.

**Conclusion: the signal is real, statistically significant in-sample, does not
clear realistic transaction costs, and stopped working around February 2026.**

## The trade

Enter near the close (15:55 New York), exit at the next opening auction. Rank the
cross-section daily, go long the top decile and short the bottom. Holding period
~17.5 hours, turnover 100% per day.

## Datasets

| | Bloomberg (BQuant) | IBKR (`ib_loader_v2.py`) |
|---|---|---|
| Sessions | 529 | 504 |
| Tickers | 503 | 149 |
| Entry price | daily close (proxy) | **true 15:55 print** |
| Intraday features | none available | `late_ret` (last 30 min) |
| Dividend adjustment | native | derived, then applied |

Bloomberg's BQL exposes daily prices only — `frq` accepts `D, W, M, Q, S, Y` and
nothing intraday — so the 15:55 entry required a separate 27-hour pull of 1-minute
bars from Interactive Brokers.

## Results

Bloomberg, 350 out-of-sample sessions:

| Variant | IC | t | gross bp | net bp |
|---|---|---|---|---|
| raw | 0.0226 | 1.90 | 7.70 | −4.30 |
| sector-neutral | 0.0222 | 2.71 | 5.95 | −6.05 |
| ex-earnings + sector-neutral | 0.0226 | 2.74 | 6.40 | −5.60 |

IBKR, 198–218 out-of-sample sessions on the research split:

| Variant | IC | t | gross bp | net bp |
|---|---|---|---|---|
| full (23 features) | 0.0600 | 5.50 | 10.52 | −1.48 |
| full without `late_ret` | 0.0408 | 3.26 | 4.06 | −7.94 |
| minimal (2 features) | 0.0480 | 5.16 | 16.47 | **+4.47** |

Round-trip cost assumed 12 bp.

## Findings

**`late_ret` carries real information.** Adding the last-30-minute return lifted IC
from 0.041 to 0.060 and the t-statistic from 3.26 to 5.50. This is the one feature
Bloomberg data structurally cannot provide, and it justified the IBKR download.

**Fewer features beat more.** Two features (`intraday_ret`, `late_ret`) produced a
better tradeable spread than twenty-three (16.5 vs 10.5 bp gross) despite a lower
IC, because the larger set carries far more factor variance (IC sd 0.138 vs 0.154).

**The effect is regime-dependent.** On the full IBKR panel, every variant collapsed
in the final 126 sessions: `full` went IC +0.060 (t=5.50) to +0.003 (t=0.20). The
same collapse appeared independently in the Bloomberg data over the same calendar
period. Both were walk-forward out-of-sample, so this is decay or regime change,
not overfitting.

**Decile monotonicity is poor.** In the best variant, D1 (+4.56 bp) outranks D2
through D6, and essentially all the spread comes from D10 (+15.81 bp). A long/short
book shorts D1 — the leg that is not working.

**Costs are the binding constraint, not signal.** At 100% daily turnover, four
one-way trades per unit of capital per day. A 6–16 bp gross edge against a 12 bp
round trip leaves nothing reliable.

## Method notes

Walk-forward throughout: models refit every 21 sessions on a rolling window and
predict only forward. Features are cross-sectionally standardised within each
session. The training target is session-demeaned; P&L uses raw returns.

`seal_holdout.py` physically splits the panel so the holdout cannot be read by
accident, and refuses to unseal without a pre-registered specification. This
matters: across six variants, in-sample IC ran 0.048–0.060 at t>5 while the true
out-of-sample value was ~0.005 at t≈0.4 — an order of magnitude apart.

## Known limitations

- **Survivorship bias.** Current index membership applied to historical windows.
  Point-in-time membership (`INDX_MWEIGHT_HIST`) not implemented.
- **The opening auction is not a price you can reliably take.** Adverse selection
  at the open is asymmetric; realised fills will trail the backtest.
- **Borrow cost and short availability are not modelled.**
- **Multiple testing.** Many variants were run. The sealed holdout exists to give
  one clean answer, and has not been opened.

## Repository

```
ib_loader_v2.py          IBKR 1-minute bar loader — pacing, resume, ex-div correction
seal_holdout.py          Physical holdout sealing with pre-registration
IB_Overnight_Model.ipynb Walk-forward modelling and evaluation
notebooks/               Bloomberg (BQuant) data export and earlier iterations
docs/RESEARCH_LOG.md     Chronological record, including what failed
```

## Data

**Not included, deliberately.** Bloomberg and IBKR both restrict redistribution of
derived data. Reproducing this requires a Bloomberg terminal with BQuant and an
IBKR account with TWS running. The loaders regenerate everything from source.

## Reproducing

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 1. IBKR bars (TWS must be running, API enabled)
python ib_loader_v2.py fetch --universe tickers.txt --months 24 --port 7496
python ib_loader_v2.py build --exdiv exdiv.csv

# 2. Bloomberg exports — run notebooks/BQuant_Export_v2.ipynb inside BQuant

# 3. Seal a holdout before modelling
python seal_holdout.py seal --sessions 126

# 4. Model
jupyter notebook IB_Overnight_Model.ipynb
```

Expect ~27 hours for a 150-ticker, 24-month IBKR pull (~27s per ticker-month).
