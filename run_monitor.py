"""Monitoring roll-up: are the jobs clean and are the dead-letter queues empty?

Exit code: 0 when HEALTHY, 1 when DEGRADED.

Usage: python run_monitor.py
"""

from __future__ import annotations

import argparse
import json
import sys

from src.config import load_config
from src.monitoring.health import run_monitor


def main() -> int:
    parser = argparse.ArgumentParser(description="Run-manifest and DLQ health roll-up")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    parser.add_argument("--json", action="store_true", help="print raw JSON only")
    args = parser.parse_args()

    cfg = load_config(args.root)
    result = run_monitor(cfg=cfg, root=args.root)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "HEALTHY" else 1

    runs = result["runs"]
    print(f"\nMONITOR {result['runId']}  —  {result['status']}")
    print("-" * 78)
    print(f"  runs recorded: {runs['total']}   by status: {runs['byStatus']}")
    print(f"  dead letters:  {result['dlq']['totalEvents']}"
          + ("  " + str(result['dlq']['locations']) if result["dlq"]["locations"] else ""))
    if runs["degraded"]:
        print("  DEGRADED RUNS:")
        for run in runs["degraded"]:
            print(f"    {run['job']:<16} {run['status']:<26} errors {run['errors']}  {run['runId']}")
    print()
    return 0 if result["status"] == "HEALTHY" else 1


if __name__ == "__main__":
    sys.exit(main())
