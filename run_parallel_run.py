"""Parallel run: reconcile the new AP pipeline against the legacy process.

Usage:
  python run_parallel_run.py
  python run_parallel_run.py --inject legacy_drift   # unexpected variance
"""

from __future__ import annotations

import argparse
import json
import sys

from src.config import load_config
from src.controls.parallel_run import run_parallel_run


def main() -> int:
    parser = argparse.ArgumentParser(description="New vs legacy AP parallel run")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    parser.add_argument("--inject", default="", help="comma-separated faults: legacy_drift")
    parser.add_argument("--json", action="store_true", help="print raw JSON only")
    args = parser.parse_args()

    inject = [s.strip() for s in args.inject.split(",") if s.strip()]
    cfg = load_config(args.root)
    result = run_parallel_run(cfg=cfg, root=args.root, inject=inject)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "PASS" else 1

    c = result["counts"]
    t = result["totals"]
    print(f"\nPARALLEL RUN {result['runId']}  —  OVERALL: {result['status']}")
    print(f"compared {c['compared']}  ok {c['ok']}  expected {c['expected']}  "
          f"unexpected {c['unexpected']}")
    print(f"legacy {t['legacyAmount']:,.2f}  new {t['newAmount']:,.2f}  "
          f"variance {t['variance']:,.2f}")
    print("-" * 78)
    for item in result["unexpectedVariances"][:10]:
        print(f"[UNEXPECTED] {item['voucherId']}  {item['supplierCode']}  "
              f"legacy {item['legacyAmount']:,.2f} new {item['newAmount']:,.2f}  "
              f"delta {item['delta']:,.2f}")
    print(f"\nreport: {result.get('outputPath', '(not written)')}\n")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
