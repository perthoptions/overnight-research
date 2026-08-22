#!/usr/bin/env python3
"""
Realised volatility panel from the cached IBKR 1-minute bars.

    python rv_builder.py build
    python rv_builder.py build --sampling 5 --horizons 5 21 63

Produces ib_out/rv_panel.parquet with, per ticker-session:

    rv5_ann      realised vol, 5-minute sampling, annualised
    rv1_ann      realised vol, 1-minute sampling (noisier; for comparison)
    bv_ann       bipower variation -- the continuous part, jump-robust
    jump_frac    (RV - BV) / RV, the share of variance from jumps
    bns_z        Barndorff-Nielsen & Shephard jump statistic
    on_var_ann   overnight (close-to-open) variance, annualised
    plus rolling means and FORWARD realised vol at each horizon.

WHY 5-MINUTE SAMPLING
1-minute returns are contaminated by bid-ask bounce, which biases realised
variance upward. Sampling every 5th bar is the standard compromise between
that bias and estimation error. Both are computed so you can see the gap --
rv1_ann running well above rv5_ann is the microstructure noise showing.

WHAT THIS IS AND IS NOT
Forecasting realised vol is easy: R^2 around 0.5-0.7, versus ~0.001 for
returns. That is NOT an edge by itself. The edge, if any, is in the spread
between a good RV forecast and what the options market implies -- and much of
that spread is the variance risk premium, which is compensation for genuine
risk rather than mispricing. This builder produces the RV half only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

CACHE = Path("ib_cache")
OUT = Path("ib_out")
NY = "America/New_York"
MINUTES_PER_YEAR = 252


def session_stats(day: pd.DataFrame, sampling: int) -> dict | None:
    """Realised measures for one ticker-session from its 1-minute bars."""
    px = day["close"].to_numpy(dtype=float)
    if len(px) < 60 or np.any(px <= 0):
        return None

    # --- 1-minute log returns
    r1 = np.diff(np.log(px))
    rv1 = float(np.sum(r1 ** 2))

    # --- sampled log returns (every `sampling`-th bar)
    ps = px[::sampling]
    if len(ps) < 12:
        return None
    r = np.diff(np.log(ps))
    n = len(r)
    rv = float(np.sum(r ** 2))

    # --- bipower variation: jump-robust estimate of the continuous part
    mu1 = np.sqrt(2.0 / np.pi)
    bv = float((mu1 ** -2) * (n / (n - 1)) * np.sum(np.abs(r[1:]) * np.abs(r[:-1])))

    # --- Barndorff-Nielsen & Shephard jump test (tri-power quarticity)
    from math import gamma
    mu43 = 2 ** (2 / 3) * gamma(7 / 6) / gamma(0.5)
    if n > 4:
        tp = np.sum((np.abs(r[2:]) ** (4 / 3)) * (np.abs(r[1:-1]) ** (4 / 3))
                    * (np.abs(r[:-2]) ** (4 / 3)))
        tq = n * (mu43 ** -3) * (n / (n - 2)) * float(tp)
    else:
        tq = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        rj = (rv - bv) / rv if rv > 0 else np.nan
        denom = np.sqrt(((np.pi ** 2) / 4 + np.pi - 5) * (1.0 / n) * max(tq / (bv ** 2), 1.0))
        bns_z = rj / denom if denom and np.isfinite(denom) and denom > 0 else np.nan

    return {
        "rv": rv, "rv1": rv1, "bv": min(bv, rv),
        "jump_frac": float(np.clip(rj, 0, 1)) if np.isfinite(rj) else np.nan,
        "bns_z": float(bns_z) if np.isfinite(bns_z) else np.nan,
        "n_ret": n, "n_bars": len(px),
        "open_px": float(px[0]), "close_px": float(px[-1]),
    }


def build_ticker(path: Path, sampling: int) -> pd.DataFrame:
    b = pd.read_parquet(path)
    b["date"] = pd.to_datetime(b["date"], utc=True).dt.tz_convert(NY)
    b["session"] = b["date"].dt.normalize().dt.tz_localize(None)
    b = b.sort_values("date")

    rows = []
    for sess, day in b.groupby("session", sort=True):
        s = session_stats(day, sampling)
        if s:
            s["session"] = sess
            rows.append(s)
    if not rows:
        return pd.DataFrame()

    d = pd.DataFrame(rows).sort_values("session").reset_index(drop=True)
    d["ticker"] = path.stem

    # overnight variance: previous close -> this open
    d["on_ret"] = np.log(d["open_px"] / d["close_px"].shift(1))
    d["on_var"] = d["on_ret"] ** 2

    for c, out in [("rv", "rv5_ann"), ("rv1", "rv1_ann"), ("bv", "bv_ann"),
                   ("on_var", "on_var_ann")]:
        d[out] = np.sqrt(d[c].clip(lower=0) * MINUTES_PER_YEAR) * 100  # vol points
    return d


def add_features(p: pd.DataFrame, horizons) -> pd.DataFrame:
    p = p.sort_values(["ticker", "session"]).reset_index(drop=True)
    g = p.groupby("ticker")
    for w in (5, 21, 63):
        p[f"rv5_ma{w}"] = g["rv5_ann"].transform(
            lambda s: s.rolling(w, min_periods=max(3, w // 2)).mean())
        p[f"bv_ma{w}"] = g["bv_ann"].transform(
            lambda s: s.rolling(w, min_periods=max(3, w // 2)).mean())
    p["jump_ma21"] = g["jump_frac"].transform(lambda s: s.rolling(21, min_periods=10).mean())
    p["vol_of_vol_21"] = g["rv5_ann"].transform(lambda s: s.rolling(21, min_periods=10).std())
    p["rv_ratio_5_63"] = p["rv5_ma5"] / p["rv5_ma63"].replace(0, np.nan)
    p["noise_ratio"] = p["rv1_ann"] / p["rv5_ann"].replace(0, np.nan)

    # FORWARD realised vol -- the modelling target
    piv = p.pivot(index="session", columns="ticker", values="rv5_ann")
    for h in horizons:
        fwd = (piv.shift(-1).rolling(h, min_periods=max(3, h // 2)).mean()
                  .shift(-(h - 1)))
        p = p.merge(fwd.stack().rename(f"fwd_rv{h}").reset_index(),
                    on=["session", "ticker"], how="left")
    return p


def build(args):
    files = sorted(CACHE.glob("*.parquet"))
    if not files:
        sys.exit(f"no cached bars in {CACHE}/ -- run ib_loader_v2.py fetch first")
    OUT.mkdir(exist_ok=True)
    print(f"building realised-vol panel from {len(files)} tickers "
          f"({args.sampling}-minute sampling)")

    parts = []
    for i, f in enumerate(files, 1):
        try:
            d = build_ticker(f, args.sampling)
            if len(d):
                parts.append(d)
        except Exception as e:
            print(f"   {f.stem}: {type(e).__name__}: {e}")
        if i % 25 == 0:
            print(f"   {i}/{len(files)}", flush=True)

    p = pd.concat(parts, ignore_index=True)
    p = add_features(p, args.horizons)
    keep = ["ticker", "session", "rv5_ann", "rv1_ann", "bv_ann", "on_var_ann",
            "jump_frac", "bns_z", "noise_ratio", "n_ret", "n_bars",
            "rv5_ma5", "rv5_ma21", "rv5_ma63", "bv_ma21", "jump_ma21",
            "vol_of_vol_21", "rv_ratio_5_63"] + [f"fwd_rv{h}" for h in args.horizons]
    p = p[[c for c in keep if c in p.columns]]

    path = OUT / "rv_panel.parquet"
    p.to_parquet(path, index=False)
    print(f"\nwrote {path}  ({len(p):,} rows, {p['ticker'].nunique()} tickers, "
          f"{p['session'].nunique()} sessions)")
    report(p, args.horizons)


def report(p: pd.DataFrame, horizons):
    print("\n" + "=" * 70)
    print("DIAGNOSTIC")
    print("=" * 70)
    print(f"sessions      : {p['session'].nunique():,}")
    print(f"tickers       : {p['ticker'].nunique():,}")
    print(f"median n_ret  : {p['n_ret'].median():.0f} per session "
          f"(78 = full day at 5-minute sampling)")

    print("\nannualised vol, percentage points:")
    print(p[["rv5_ann", "rv1_ann", "bv_ann"]].describe(
        percentiles=[.05, .5, .95]).round(2).to_string())

    nr = p["noise_ratio"].median()
    print(f"\nmicrostructure noise: median rv1/rv5 = {nr:.3f}")
    if nr > 1.15:
        print("   1-minute RV is materially inflated by bid-ask bounce, as expected.")
        print("   Use rv5_ann. This is exactly why 5-minute sampling is standard.")
    elif nr < 0.9:
        print("   WARNING: 1-minute RV BELOW 5-minute. Unexpected -- check the bars.")

    print(f"\njump share of variance: median {p['jump_frac'].median():.3f}, "
          f"95th {p['jump_frac'].quantile(.95):.3f}")
    sig = (p["bns_z"] > 2.58).mean()
    print(f"sessions with significant jumps (BNS z > 2.58): {sig:.2%}")
    print("   Typical for liquid US large caps is roughly 5-15%.")

    print("\nPERSISTENCE — the reason vol is forecastable at all:")
    for h in horizons:
        c = p[["rv5_ma21", f"fwd_rv{h}"]].dropna()
        if len(c) > 100:
            r = c.corr().iloc[0, 1]
            print(f"   corr(RV 21d trailing, forward {h}d RV) = {r:+.3f}  "
                  f"R2 = {r**2:.3f}")
    print("\n   Compare with return predictability: R2 of order 0.001.")
    print("   This is why vol is the more tractable target -- but forecasting RV")
    print("   is not a trade. The trade needs implied vol, and most of the")
    print("   IV-RV spread is variance risk premium, not mispricing.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--sampling", type=int, default=5,
                   help="sample every Nth 1-minute bar (default 5)")
    b.add_argument("--horizons", type=int, nargs="+", default=[5, 21, 63])
    b.set_defaults(func=build)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
