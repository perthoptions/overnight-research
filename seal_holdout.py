#!/usr/bin/env python3
"""
Seal a holdout so it cannot be looked at by accident.

    python seal_holdout.py seal    --sessions 126
    python seal_holdout.py status
    python seal_holdout.py register --file spec.md
    python seal_holdout.py unseal

SEAL splits ib_out/ib_daily_panel.parquet into

    ib_out/panel_research.parquet   <- point every notebook at this one
    ib_out/SEALED/panel_holdout.parquet

and records a lock file with the split date and a SHA-256 of the sealed data.
Point your modelling notebooks at panel_research.parquet and the holdout is
simply not reachable.

REGISTER writes your pre-committed specification into the lock BEFORE you unseal.
UNSEAL refuses to run unless a specification has been registered, prints it back,
and records that the seal was broken. Breaking it twice is recorded too -- the
log is the point.

Why bother: on the overnight study, in-sample IC ran 0.048-0.060 at t>5 across
six variants while the true out-of-sample value was ~0.005 at t~0.4. Each variant
tried on the same data spends significance. A sealed holdout is the only thing
that gives a straight answer at the end.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PANEL = Path("ib_out/ib_daily_panel.parquet")
RESEARCH = Path("ib_out/panel_research.parquet")
SEALED_DIR = Path("ib_out/SEALED")
HOLDOUT = SEALED_DIR / "panel_holdout.parquet"
LOCK = Path("ib_out/holdout_lock.json")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()[:16]


def seal(args):
    if LOCK.exists():
        lock = json.loads(LOCK.read_text())
        sys.exit(f"Already sealed on {lock['sealed_at']} at {lock['cut_date']}.\n"
                 f"Run `status`, or delete {LOCK} to start over "
                 f"(and be honest with yourself about why).")
    if not PANEL.exists():
        sys.exit(f"{PANEL} not found -- run `ib_loader_v2.py build` first")

    p = pd.read_parquet(PANEL)
    p["session"] = pd.to_datetime(p["session"]).dt.normalize()
    sess = pd.Index(sorted(p["session"].unique()))
    if len(sess) <= args.sessions + 100:
        sys.exit(f"only {len(sess)} sessions; sealing {args.sessions} leaves too few")

    cut = sess[-args.sessions]
    research, hold = p[p["session"] < cut], p[p["session"] >= cut]

    SEALED_DIR.mkdir(parents=True, exist_ok=True)
    research.to_parquet(RESEARCH, index=False)
    hold.to_parquet(HOLDOUT, index=False)

    lock = {
        "sealed_at": datetime.now().isoformat(timespec="seconds"),
        "cut_date": str(cut.date()),
        "research_sessions": int(research["session"].nunique()),
        "holdout_sessions": int(hold["session"].nunique()),
        "research_rows": len(research),
        "holdout_rows": len(hold),
        "holdout_sha256": _sha(HOLDOUT),
        "specification": None,
        "registered_at": None,
        "unsealed_at": [],
    }
    LOCK.write_text(json.dumps(lock, indent=2))

    print(f"SEALED at {cut.date()}")
    print(f"  research : {lock['research_sessions']:>4} sessions, "
          f"{lock['research_rows']:>7,} rows -> {RESEARCH}")
    print(f"  holdout  : {lock['holdout_sessions']:>4} sessions, "
          f"{lock['holdout_rows']:>7,} rows -> {HOLDOUT}")
    print(f"\nPoint every notebook at {RESEARCH}.")
    print("Iterate freely there. When you have ONE specification you believe in:")
    print("    python seal_holdout.py register --file spec.md")
    print("    python seal_holdout.py unseal")


def status(args):
    if not LOCK.exists():
        print("No holdout sealed. Run: python seal_holdout.py seal --sessions 126")
        return
    lock = json.loads(LOCK.read_text())
    print(json.dumps(lock, indent=2))
    if lock["unsealed_at"]:
        print(f"\n!! Unsealed {len(lock['unsealed_at'])} time(s). "
              f"Every look after the first is in-sample.")
    elif lock["specification"]:
        print("\nSpecification registered; ready to unseal ONCE.")
    else:
        print("\nSealed and clean. No specification registered yet.")


def register(args):
    if not LOCK.exists():
        sys.exit("Nothing sealed yet.")
    lock = json.loads(LOCK.read_text())
    if lock["specification"]:
        print("A specification is already registered:\n")
        print(lock["specification"])
        if input("\nReplace it? [y/N] ").strip().lower() != "y":
            return
    text = Path(args.file).read_text() if args.file else None
    if not text or len(text.strip()) < 80:
        sys.exit("Specification too short. State the feature set, model, target, "
                 "universe filters, cost assumption, and the pass/fail threshold "
                 "you commit to BEFORE looking.")
    lock["specification"] = text
    lock["registered_at"] = datetime.now().isoformat(timespec="seconds")
    LOCK.write_text(json.dumps(lock, indent=2))
    print(f"Registered at {lock['registered_at']}. Now: python seal_holdout.py unseal")


def unseal(args):
    if not LOCK.exists():
        sys.exit("Nothing sealed.")
    lock = json.loads(LOCK.read_text())
    if not lock["specification"]:
        sys.exit("No specification registered. Register one first -- the whole "
                 "point is that the test is fixed before the data is seen.")
    if _sha(HOLDOUT) != lock["holdout_sha256"]:
        print("WARNING: holdout file has changed since sealing.")

    print("=" * 66)
    print("SPECIFICATION REGISTERED " + (lock["registered_at"] or ""))
    print("=" * 66)
    print(lock["specification"])
    print("=" * 66)
    if lock["unsealed_at"]:
        print(f"\nAlready unsealed {len(lock['unsealed_at'])} time(s): "
              f"{lock['unsealed_at']}")
        print("Anything you find now is in-sample. Proceed only with that in mind.")
    if input("\nRun exactly this specification, unchanged? [y/N] ").strip().lower() != "y":
        print("Aborted. Holdout intact.")
        return

    lock["unsealed_at"].append(datetime.now().isoformat(timespec="seconds"))
    LOCK.write_text(json.dumps(lock, indent=2))
    print(f"\nHoldout available at {HOLDOUT}")
    print("Run the registered specification. Report whatever it gives you.")


SPEC_TEMPLATE = """# Pre-registered specification

## Hypothesis
(e.g. H1 -- the cross-sectional signal survives a 5-day holding period because
transaction cost per day falls from 12bp to 2.4bp)

## Target
target_5d_forward, session-demeaned for training, raw for P&L

## Features
Exact list. No additions after registration.

## Model
HistGradientBoostingRegressor, squared_error, lr 0.045, 180 iters,
15 leaves, min_samples_leaf 80, l2 2.0, seed 42.
Walk-forward, refit every 21 sessions, min 160 training sessions.

## Universe filters
Exclude earnings windows +/-2 days. Minimum 40 names per session.

## Costs
12 bp round trip, divided by holding period in days.

## Pass / fail, committed in advance
PASS if holdout mean IC > 0.015 AND IC t-stat > 2.0 AND net bp/day > 0.
Anything else is a FAIL and the hypothesis is dropped.

## What I will NOT do
Re-tune and re-test. One look.
"""


def template(args):
    p = Path("spec_template.md")
    if p.exists():
        sys.exit(f"{p} already exists")
    p.write_text(SPEC_TEMPLATE)
    print(f"wrote {p} -- edit it, then: python seal_holdout.py register --file {p}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seal"); s.add_argument("--sessions", type=int, default=126)
    s.set_defaults(func=seal)
    sub.add_parser("status").set_defaults(func=status)
    r = sub.add_parser("register"); r.add_argument("--file", required=True)
    r.set_defaults(func=register)
    sub.add_parser("unseal").set_defaults(func=unseal)
    sub.add_parser("template").set_defaults(func=template)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
