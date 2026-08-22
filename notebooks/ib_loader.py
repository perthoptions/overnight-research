#!/usr/bin/env python3
"""
IBKR 1-minute bar loader for the 15:55 -> next-open study.

Two modes:

    python ib_loader.py fetch --universe tickers.txt --months 12
    python ib_loader.py build

FETCH walks backwards in 1-month chunks (the largest IB accepts for 1-min bars),
paces requests under IB's ~60-per-10-minute limit, caches one parquet per ticker,
and resumes cleanly if interrupted.

BUILD turns the cached bars into a daily panel with the true 15:55 entry price,
late-session momentum, and the next-open target -- plus the
bquant_1555_exact_snapshots.pkl the BQuant notebook already knows how to read.

KNOWN LIMITATIONS -- read these before trusting output:
  * IB TRADES bars are SPLIT-adjusted but NOT DIVIDEND-adjusted. Every ex-div
    date becomes a fake overnight gap equal to the dividend. Run
    `build --exdiv exdiv.csv` with columns ticker,ex_date,amount to correct it,
    or those dates will manufacture signal in exactly the target you model.
  * The 09:30 bar is a 1-minute aggregate, NOT necessarily the opening auction
    cross. Compare against Bloomberg PX_OPEN before relying on it (see
    `build --report`).
  * useRTH=True, so bars run 09:30-15:59 New York only.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

NY = "America/New_York"
CACHE = Path("ib_cache")
OUT = Path("ib_out")

# IB allows ~60 historical requests per 10 minutes. 10.5s spacing keeps us under
# it with margin; pacing violations (error 420) cost far more than the wait.
REQUEST_SPACING_S = 10.5
CHUNK = "1 M"
BAR = "1 min"

ENTRY_MIN = "15:55"
LATE_WINDOW_MIN = 30
OPEN_MIN = "09:30"


# ------------------------------------------------------------------ fetch
def _import_ib():
    """ib_async needs Python 3.10+; ib_insync is the 3.9-compatible predecessor.
    Both expose the same IB/Stock/util API for everything used here."""
    try:
        from ib_async import IB, Stock, util
        return IB, Stock, util, "ib_async"
    except ImportError:
        pass
    try:
        from ib_insync import IB, Stock, util
        import nest_asyncio
        nest_asyncio.apply()          # ib_insync + older asyncio needs this
        return IB, Stock, util, "ib_insync"
    except ImportError as e:
        raise SystemExit(
            "Neither ib_async nor ib_insync is installed.\n"
            "  Python 3.10+ : pip install ib_async\n"
            "  Python 3.9   : pip install ib_insync nest_asyncio"
        ) from e


def fetch(args):
    IB, Stock, util, _lib = _import_ib()
    print(f"using {_lib}")

    tickers = _load_universe(args.universe, args.limit)
    CACHE.mkdir(exist_ok=True)
    print(f"universe : {len(tickers)} tickers")
    print(f"history  : {args.months} months  -> ~{args.months} chunks each")
    print(f"requests : ~{len(tickers) * args.months:,} "
          f"-> ~{len(tickers) * args.months * REQUEST_SPACING_S / 3600:.1f} h\n")

    ib = IB()
    ib.connect(args.host, args.port, clientId=args.client_id, timeout=20)
    print(f"connected: {ib.isConnected()} on {args.host}:{args.port}\n")

    done = skipped = failed = 0
    t0 = time.time()

    for i, sym in enumerate(tickers, 1):
        path = CACHE / f"{sym}.parquet"
        if path.exists() and not args.force:
            skipped += 1
            continue

        contract = Stock(sym, "SMART", "USD")
        try:
            ib.qualifyContracts(contract)
        except Exception as e:
            print(f"[{i:>4}/{len(tickers)}] {sym:<6} qualify failed: {e}")
            failed += 1
            continue

        frames = []
        end_dt = ""  # "" == now
        for chunk_i in range(args.months):
            bars = None
            for attempt in range(3):
                try:
                    bars = ib.reqHistoricalData(
                        contract, endDateTime=end_dt, durationStr=CHUNK,
                        barSizeSetting=BAR, whatToShow="TRADES",
                        useRTH=True, formatDate=2)  # formatDate=2 -> UTC epoch
                    break
                except Exception as e:
                    wait = 30 * (attempt + 1)
                    print(f"      {sym} chunk {chunk_i}: {type(e).__name__}; "
                          f"retry in {wait}s")
                    time.sleep(wait)
            if not bars:
                break

            df = util.df(bars)
            if df is None or df.empty:
                break
            frames.append(df)
            earliest = pd.to_datetime(df["date"]).min()
            end_dt = earliest.to_pydatetime()
            time.sleep(REQUEST_SPACING_S)

        if not frames:
            print(f"[{i:>4}/{len(tickers)}] {sym:<6} NO DATA")
            failed += 1
            continue

        out = pd.concat(frames, ignore_index=True)
        out["date"] = _to_ny(out["date"])
        out = out.drop_duplicates("date").sort_values("date").reset_index(drop=True)
        out["ticker"] = sym
        out.to_parquet(path, index=False)

        done += 1
        rate = (time.time() - t0) / max(done, 1)
        left = (len(tickers) - i) * rate / 3600
        print(f"[{i:>4}/{len(tickers)}] {sym:<6} {len(out):>7,} bars  "
              f"{out['date'].min().date()} -> {out['date'].max().date()}  "
              f"(~{left:.1f}h left)", flush=True)

    ib.disconnect()
    print(f"\nfetched {done}, skipped {skipped}, failed {failed}")


def _to_ny(s):
    ts = pd.to_datetime(s, utc=True, errors="coerce")
    return ts.dt.tz_convert(NY)


def _load_universe(path, limit):
    syms = [ln.strip().upper() for ln in Path(path).read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]
    # tolerate "AAPL US Equity" style input
    syms = [s.split()[0] for s in syms]
    seen, out = set(), []
    for s in syms:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out[:limit] if limit else out


# ------------------------------------------------------------------ build
def daily_from_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Collapse 1-min bars for one ticker into a daily row with a true 15:55 entry."""
    b = bars.copy()
    b["date"] = pd.to_datetime(b["date"], utc=True).dt.tz_convert(NY)
    b["session"] = b["date"].dt.normalize().dt.tz_localize(None)
    b["hhmm"] = b["date"].dt.strftime("%H:%M")

    rows = []
    late_from = (pd.Timestamp(f"2000-01-01 {ENTRY_MIN}")
                 - pd.Timedelta(minutes=LATE_WINDOW_MIN)).strftime("%H:%M")

    for session, d in b.groupby("session", sort=True):
        d = d.sort_values("date")
        if len(d) < 100:                       # half-days / bad sessions
            continue

        at_entry = d[d["hhmm"] == ENTRY_MIN]
        if len(at_entry):
            entry = float(at_entry["open"].iloc[0])
        else:
            before = d[d["hhmm"] <= ENTRY_MIN]
            if before.empty:
                continue
            entry = float(before["close"].iloc[-1])

        at_open = d[d["hhmm"] == OPEN_MIN]
        px_open = float(at_open["open"].iloc[0]) if len(at_open) else float(d["open"].iloc[0])

        lw = d[(d["hhmm"] >= late_from) & (d["hhmm"] <= ENTRY_MIN)]
        px_late = float(lw["open"].iloc[0]) if len(lw) else entry

        upto = d[d["hhmm"] <= ENTRY_MIN]
        rows.append({
            "session": session,
            "entry_price": entry,
            "px_open": px_open,
            "px_close": float(d["close"].iloc[-1]),
            "high_to_entry": float(upto["high"].max()),
            "low_to_entry": float(upto["low"].min()),
            "vol_to_entry": float(upto["volume"].sum()),
            "late_ret": entry / px_late - 1.0 if px_late else None,
            "n_bars": len(d),
        })

    out = pd.DataFrame(rows)
    if len(out):
        out["ticker"] = bars["ticker"].iloc[0]
    return out


def apply_exdiv(panel: pd.DataFrame, exdiv_path: str) -> pd.DataFrame:
    """Remove the fake overnight gap created by unadjusted dividends."""
    ex = pd.read_csv(exdiv_path)
    ex.columns = [c.strip().lower() for c in ex.columns]
    ex["ex_date"] = pd.to_datetime(ex["ex_date"]).dt.normalize()
    ex["ticker"] = ex["ticker"].str.split().str[0].str.upper()
    ex = ex.groupby(["ticker", "ex_date"], as_index=False)["amount"].sum()

    p = panel.merge(ex.rename(columns={"ex_date": "session"}),
                    on=["ticker", "session"], how="left")
    p["amount"] = p["amount"].fillna(0.0)
    # the dividend is dropped from the price at the ex-date open
    p["px_open_adj"] = p["px_open"] + p["amount"]
    n = int((p["amount"] > 0).sum())
    print(f"   ex-div adjustments applied: {n:,} stock-sessions")
    return p


def build(args):
    files = sorted(CACHE.glob("*.parquet"))
    if not files:
        sys.exit(f"no cached bars in {CACHE}/ -- run `fetch` first")
    OUT.mkdir(exist_ok=True)
    print(f"building from {len(files)} cached tickers")

    panels = []
    for i, f in enumerate(files, 1):
        try:
            panels.append(daily_from_bars(pd.read_parquet(f)))
        except Exception as e:
            print(f"   {f.stem}: {type(e).__name__}: {e}")
        if i % 25 == 0:
            print(f"   {i}/{len(files)}", flush=True)

    panel = pd.concat([p for p in panels if len(p)], ignore_index=True)
    panel = panel.sort_values(["ticker", "session"]).reset_index(drop=True)

    if args.exdiv:
        panel = apply_exdiv(panel, args.exdiv)
        open_col = "px_open_adj"
    else:
        open_col = "px_open"
        print("   WARNING: no --exdiv file. Ex-dividend dates will appear as "
              "fake overnight gaps.")

    # next-session open, per ticker, on the actual session grid
    sess = pd.Index(sorted(panel["session"].unique()))
    cal = pd.DataFrame({"session": sess[:-1], "next_session": sess[1:]})
    panel = panel.merge(cal, on="session", how="left")
    nxt = panel[["ticker", "session", open_col]].rename(
        columns={"session": "next_session", open_col: "next_open"})
    panel = panel.merge(nxt, on=["ticker", "next_session"], how="left")

    panel["target_1555_to_open"] = panel["next_open"] / panel["entry_price"] - 1
    panel["target_close_to_open"] = panel["next_open"] / panel["px_close"] - 1
    panel["entry_vs_close_bp"] = (panel["entry_price"] / panel["px_close"] - 1) * 1e4

    pq = OUT / "ib_daily_panel.parquet"
    panel.to_parquet(pq, index=False)
    print(f"\nwrote {pq}  ({len(panel):,} rows, "
          f"{panel['ticker'].nunique()} tickers, {panel['session'].nunique()} sessions)")

    # snapshot pickle in the BQuant notebook's exact format
    snap = panel[["ticker", "session", "entry_price"]].copy()
    snap["ticker"] = snap["ticker"] + " US Equity"
    snap["captured_at_ny"] = (pd.to_datetime(snap["session"])
                              .dt.strftime(f"%Y-%m-%dT{ENTRY_MIN}:00-04:00"))
    pkl = OUT / "bquant_1555_exact_snapshots.pkl"
    snap.to_pickle(pkl)
    print(f"wrote {pkl}  ({len(snap):,} rows) -- copy into the BQuant project dir")

    _report(panel)


def _report(panel: pd.DataFrame):
    print("\n" + "=" * 66)
    print("DIAGNOSTIC")
    print("=" * 66)
    print(f"sessions          : {panel['session'].nunique():,}")
    print(f"tickers           : {panel['ticker'].nunique():,}")
    print(f"rows              : {len(panel):,}")
    print(f"bars/session (med): {panel['n_bars'].median():.0f}  (390 = full RTH day)")

    e = panel["entry_vs_close_bp"].dropna()
    print(f"\n15:55 vs close    : mean {e.mean():+.2f} bp, sd {e.std():.2f} bp, "
          f"|median| {e.abs().median():.2f} bp")
    print("   If this sd is small relative to overnight-return sd (~100bp),")
    print("   the close proxy was fine and 15:55 adds little.")

    for c in ["target_1555_to_open", "target_close_to_open"]:
        t = panel[c].dropna()
        print(f"{c:<24}: n={len(t):,}  sd={t.std() * 1e4:,.0f} bp  "
              f"mean={t.mean() * 1e4:+.2f} bp")

    corr = panel[["target_1555_to_open", "target_close_to_open"]].dropna().corr().iloc[0, 1]
    print(f"\ncorrelation of the two targets: {corr:.5f}")
    print("   Near 1.0 means the 15:55 entry changes essentially nothing and the")
    print("   550-session Bloomberg result already answered the question.")

    big = panel["target_close_to_open"].abs() > 0.10
    print(f"\novernight moves >10%: {int(big.sum()):,} "
          f"({big.mean():.3%}) -- check these are real, not unadjusted dividends")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch")
    f.add_argument("--universe", required=True, help="text file, one symbol per line")
    f.add_argument("--months", type=int, default=12)
    f.add_argument("--limit", type=int, default=None)
    f.add_argument("--host", default="127.0.0.1")
    f.add_argument("--port", type=int, default=7496)
    f.add_argument("--client-id", type=int, default=11)
    f.add_argument("--force", action="store_true", help="re-fetch cached tickers")
    f.set_defaults(func=fetch)

    b = sub.add_parser("build")
    b.add_argument("--exdiv", default=None,
                   help="CSV with columns ticker,ex_date,amount")
    b.add_argument("--report", action="store_true")
    b.set_defaults(func=build)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
