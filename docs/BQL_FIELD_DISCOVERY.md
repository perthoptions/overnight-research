# BQL Field Discovery

**Read this before writing any new BQL query.** Guessing field names and parameters
cost more time on this project than every model fitted. Two lines of Python avoid it.

## The workflow

### 1. `dir(bq.data)` — what fields exist

```python
names = sorted(n for n in dir(bq.data) if not n.startswith('_'))
print(f"{len(names):,} data items")          # ~51,000

def find(*keywords, avoid=()):
    hits = [n for n in names
            if any(k in n.lower() for k in keywords)
            and not any(a in n.lower() for a in avoid)]
    for h in hits:
        print("   ", h)
    return hits

find("dvd", "divid")
find("impvol", "ivol")
find("earn", "announce")
```

### 2. `__doc__` — how to call it

```python
print(bq.data.ivol_moneyness.__doc__)
```

Returns every valid signature, every parameter, and the permitted values:

```
IVOL_MONEYNESS(PER=D, FILL=NA, DATES=0D, EXPIRY=90D, MODE)
IVOL_MONEYNESS(START=0D, END=0D, PER=D, FILL=NA, EXPIRY=90D,
               PCT_MONEYNESS <mandatory>, MODE)

EXPIRY (ENUM): TERM FOR IMPLIED VOLATILITY
    Possible values are: "180D", "1L", "1STM", "2NDM", "30D", "360D",
                         "540D", "60D", "720D", "90D".
PCT_MONEYNESS (ENUM): RATIO OF STRIKE DIVIDED BY SPOT PRICE
    Possible values are: "100", "102", "102.5", "105", "110", "120",
                         "80", "90", "95", "97", "97.5".
FILL (ENUM): FILL WHEN DATA IS MISSING
    Possible values are: "NA", "NEXT", "PREV".
```

Three things there that hours of guessing had not produced: the parameter is
`pct_moneyness` not `moneyness`, values are **strings** not integers, and
**`EXPIRY` silently defaults to 90D**.

### 3. Probe on one ticker before pulling 500

```python
d = list(bq.execute(bql.Request("IBM US Equity", {
    "iv": bq.data.ivol_moneyness(dates=RNG, expiry='30D', pct_moneyness='100')
})))[0].df().reset_index()
print(d.shape, d.columns.tolist())
print(d.head())
```

Sanity-check the level against something you already know. IBM 30-day ATM IV
around 22–26 in a normal regime; a series returning 38 is measuring something else.

---

## What this project got wrong, and what it cost

| Assumption | Reality | Cost |
|---|---|---|
| `dvd_hist_all`, `dvd_hist`, `eqy_dvd_hist_gross` | None exist | ~1h |
| `fa_period_type` / `fa_period_offset` | BQL wants `fpt` / `fpo` | ~1h |
| `dividends(dates=range)` gives history | Broadcasts one **projected** future event across every calendar day | ~1h |
| `ivol_moneyness()` is ATM | Defaults to **90-day**, unspecified moneyness → ran 1.47× realised | ~2h |
| `px_last(frq='1MIN')` | BQL is daily-only: `D, W, M, Q, S, Y` | — |

Error messages are the second-best source after `__doc__`. `InvalidParameterGroupError`
lists each parameter group and why it was rejected — that is how the intraday limit
surfaced, and how `PCT_MONEYNESS` was eventually located.

```python
try:
    bq.data.ivol_moneyness(dates=RNG, moneyness=100)
except Exception as e:
    print(str(e))        # print the FULL message; truncation hides the answer
```

---

## Traps beyond field names

**BQL returns a calendar-day grid, not trading days.** 552 rows per ticker over 550
calendar days, weekends forward-filled. `px_open` is correctly null on those fabricated
rows, which silently emptied a whole feature panel through `dropna`. Pass `fill='NA'`
and intersect with a real trading calendar (sessions on which the benchmark printed).

**Do not de-duplicate to one row per ticker.** A helper doing
`drop_duplicates(['ticker'])` collapsed 4,024 earnings dates to 503 and silently
disabled the earnings filter for an entire study. Anything with history — dividends,
earnings, corporate actions — has many rows per ticker.

**`bq.univ.members()` returns a BqlItem, not a list.** Resolve it with an actual
request before trying to chunk it:

```python
r = bq.execute(bql.Request(bq.univ.members("SPX Index"), {"nm": bq.data.name()}))
ids = list(r)[0].df().reset_index()
tickers = sorted(ids.rename(columns={"id": "ticker"})["ticker"].unique())
```

**Numerics coerce to 1970 dates.** `pd.to_datetime` on a float column returns valid-
looking epoch timestamps, so a date-column detector will happily accept junk. Skip
numeric dtypes and require the parsed years to fall in a sane range.

---

## When a field genuinely does not exist

Derive it. No BQL field returned dividend history with ex-dates and amounts, but

```
day_to_day_tot_return_gross_dvds − price return = dividend, on the ex-date
```

Validated on IBM: 7 events over 20 months, implied amounts 1.67–1.69 against an actual
quarterly dividend of 1.67–1.69, dates matching the known Feb/May/Aug/Nov schedule.

Similarly, earnings announcement dates came from `is_eps(fpt='Q', fpo=range(-10,0))`
via its `revision_date` column — the date each quarter's EPS was first reported.
Flagged sessions proved 2.10× as volatile as unflagged ones, confirming alignment.
