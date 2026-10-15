"""Seed the AP domain: suppliers, POs, goods receipts, vendor invoices.

Usage:
  python run_seed_ap.py
  python run_seed_ap.py --inject price_variance,no_po,duplicate_invoice
"""

from __future__ import annotations

import argparse
import json

from src.config import load_config
from src.sources.seed_ap import run_seed_ap


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic AP source data")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    parser.add_argument("--inject", default="",
                        help="comma-separated breaks: price_variance, no_po, duplicate_invoice")
    parser.add_argument("--reset", action="store_true",
                        help="also clear shared engine/GL/AR state (use for an AP-only run)")
    args = parser.parse_args()

    inject = [s.strip() for s in args.inject.split(",") if s.strip()]
    cfg = load_config(args.root)
    summary = run_seed_ap(cfg=cfg, root=args.root, inject=inject, reset=args.reset)

    print("taxmesh AP seed complete")
    print(json.dumps(summary, indent=2))
    if summary["injected"]:
        print("KNOWN BREAKS INJECTED — run run_ap_controls.py to watch them get caught.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
