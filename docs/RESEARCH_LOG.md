# Research Log

Chronological, including dead ends. The dead ends took most of the time and are
the more useful record.

## 1. Bloomberg-only attempt

Built a BQuant notebook ranking S&P 500 constituents by predicted overnight return.

**Failed with `Only 0 complete model sessions`.** Root cause took several rounds to
find: BQL returned a **calendar-day grid** (552 rows per ticker over 550 calendar
days) with weekends forward-filled. `px_open` was correctly null on those fabricated
rows, which made `ret_intraday` and three other features 100% NaN, which emptied the
panel through `dropna` — with no error until the model cell.

Contributing bugs found in the same pass:

- `bql_items` de-duplicated to one row per ticker, collapsing 4,024 earnings dates
  to 503. The earnings filter was silently a no-op.
- A groupby was constructed before the columns it referenced existed.
- The 15:55 capture window compared `"%H:%M"` strings with start == end == `"15:55"`
  — a 60-second window that never fired, so every entry silently used the close.
- The benchmark was matched by hardcoded string rather than the ID BQL returned.

**Lesson:** the rebuilt notebook prints a diagnostic at every stage *before* any
guard fires, and prunes low-coverage features rather than letting `dropna` empty the
panel. Silent NaN propagation is the dominant failure mode in this kind of pipeline.

## 2. Bloomberg results

Best variant reached IC 0.0226, t=2.74, gross 6.4 bp against a 12 bp round trip.
Sector-neutralisation cut IC standard deviation 31% (0.222 → 0.153) with no change
to mean IC, lifting the t-statistic from 1.90 to 2.71 — roughly a third of the IC
variance was sector exposure.

The held-out final 126 sessions gave IC −0.0015, t=−0.11, against +0.0362, t=3.47
in the tuning window.

## 3. Getting intraday data

BQL is daily-only: `frq` accepts `D, W, M, Q, S, Y`. Bloomberg's intraday store
retains ~140 calendar days regardless. `blpapi` was importable in BQuant but the
Desktop API was not reachable.

Moved to IBKR. Probing established 1-minute bars cap at ~1 month per request
(`3 M` times out) with 24 months of depth available. At IB's pacing limit the real
cost is **~27 seconds per ticker-month** — 151 tickers × 24 months ≈ 27 hours, versus
the 10.6 hours a naive 10.5s-per-request estimate suggested.

## 4. IBKR data quality

- IB `TRADES` bars are split-adjusted but **not dividend-adjusted**. Every ex-date
  appears as a fake overnight gap.
- No BQL field returns dividend history directly: `dvd_hist_all`, `dvd_hist`,
  `eqy_dvd_hist_gross` do not exist; `dvd_ex_dt` gives one row per ticker with no
  amount; `dividends` with a date range broadcasts a single *projected* future event
  across every calendar day.
- **Solution:** `day_to_day_tot_return_gross_dvds` minus the price return isolates
  the dividend on exactly the ex-date. Validated on IBM — 7 events, implied amounts
  1.67–1.69 against actual 1.67–1.69, correct dates. 1,019 adjustments applied.
- Earnings dates came from `is_eps(fpt='Q', fpo=range(-10,0))` via `revision_date`.
  BQL wants `fpt`/`fpo`, not `fa_period_type`/`fa_period_offset`. Flagged sessions
  proved 2.10× as volatile as unflagged ones, confirming alignment.

## 5. The 15:55 question

The original premise was that entering at 15:55 rather than the close would matter.
It does not, much: correlation between the two targets is **0.989**, with the entry
differing by ~19 bp on a typical day against a 126 bp overnight standard deviation.
Entering at 15:55 averaged slightly *worse* (+2.97 vs +3.63 bp) because price drifts
up into the close.

The real return on the IBKR download was `late_ret`, not the entry price.

## 6. Where it stands

Signal is real, regime-dependent, and does not clear costs at 100% daily turnover.
The sealed holdout (126 sessions from 2026-02-23) has not been opened.

Untested directions, in order of promise:

1. **Longer holding period.** Cost per day falls linearly — a 5-day hold turns 12 bp
   round-trip into 2.4 bp/day. Attacks the binding constraint directly.
2. **Long-only D10.** The short leg is the broken one; dropping it halves costs.
3. **Cheaper instrument.** Index futures round-trip under 1 bp, but one bet per night
   instead of 149 is far less powerful.
4. **Volatility rather than direction.** Returns are predictable at R²≈0.001,
   realised volatility at R²≈0.5+. Needs options data to trade.

## 8. H3a — jump mispricing: REJECTED

Premise: diffusive vol persists, jumps don't, so IV pricing total RV
overprices recently-jumped names.

Test 1 — forecasting fwd 21d RV (n=70,330, demeaned, NW 21 lags):

    total RV only      R2 0.7360
    continuous only    R2 0.7350
    continuous + jump  R2 0.7379, jump coef +8.23 (t +8.03)

Jump coefficient POSITIVE: jumps predict HIGHER future vol. Premise inverted.

Test 2 — IV on its components (n=73,242):

    bv_ma21    +1.2934 (t +88.82)   <- variance risk premium, ~29%
    jump_ma21  +2.0863 (t  +1.70)   <- IV largely ignores jumps

Combined: the market prices the persistent component and ignores the jump
share, while the jump share does forecast future vol. That implies IV may
UNDER-price jumpy names — the opposite of the hypothesis. Not pursued:
post-hoc, and the effect is ~2 vol points across the realistic jump-share
range against a t=1.70 IV response and real straddle spreads.
