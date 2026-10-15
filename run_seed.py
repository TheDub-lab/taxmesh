"""Seed the workspace with deterministic synthetic source data.

Usage:
  python run_seed.py                       # clean workspace
  python run_seed.py --inject drop_event   # known break: event never sent
  python run_seed.py --inject drop_event,dlq_event
"""

from __future__ import annotations

import argparse
import json

from src.config import load_config
from src.sources.seed import run_seed


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic taxmesh source data")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    parser.add_argument("--inject", default="",
                        help="comma-separated known breaks: drop_event, dlq_event")
    args = parser.parse_args()

    inject = [s.strip() for s in args.inject.split(",") if s.strip()]
    cfg = load_config(args.root)
    summary = run_seed(cfg=cfg, root=args.root, inject=inject)

    print("taxmesh seed complete")
    print(json.dumps(summary, indent=2))
    if summary["injected"]:
        print("KNOWN BREAKS INJECTED — run run_controls.py to watch them get caught.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
