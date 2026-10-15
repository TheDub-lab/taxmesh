"""Run SOX-style control totals / source-to-target tie-outs.

Exit code: 0 when every control passes, 1 when any control fails.

Usage: python run_controls.py
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from src.config import load_config
from src.controls.tieouts import run_tieouts
from src.integrations.engine_client import EngineClient


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Control totals and tie-outs")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    parser.add_argument("--json", action="store_true", help="print raw JSON only")
    args = parser.parse_args()

    cfg = load_config(args.root)
    client = EngineClient(base_url=cfg["engine"]["base_url"])
    try:
        client.health()
    except httpx.HTTPError:
        print(f"ERROR: tax engine not reachable at {cfg['engine']['base_url']}.")
        print("Start it first:  python run_engine.py")
        return 2

    result = run_tieouts(cfg=cfg, root=args.root, engine=client)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "PASS" else 1

    print(f"\nTIE-OUT RUN {result['runId']}  —  OVERALL: {result['status']}")
    print("-" * 78)
    for c in result["controls"]:
        marker = "PASS" if c["status"] == "PASS" else "FAIL"
        print(f"[{marker}] {c['name']:<40} {_fmt(c['left'])} vs {_fmt(c['right'])}"
              + (f"  delta {c['delta']}" if c["delta"] else ""))
    print("-" * 78)
    for row in result["byJurisdiction"]:
        marker = "PASS" if row["status"] == "PASS" else "FAIL"
        print(f"[{marker}] jurisdiction {row['jurisdictionId']:<12} "
              f"engine {row['engineTax']:>10,.2f}  gl {row['glTax']:>10,.2f}")
    exc = result["exceptions"]
    if exc["missingInEngine"] or exc["dlqEvents"] or exc["pendingEvents"]:
        print("-" * 78)
        print("EXCEPTIONS DETECTED BY CONTROLS:")
        if exc["missingInEngine"]:
            print(f"  missing in engine ({len(exc['missingInEngine'])}): "
                  f"{', '.join(exc['missingInEngine'][:10])}")
        if exc["dlqEvents"]:
            print(f"  dead-lettered events: {', '.join(exc['dlqEvents'])}")
        if exc["pendingEvents"]:
            print(f"  unprocessed events: {', '.join(exc['pendingEvents'])}")
    print(f"\nreport: {result.get('outputPath', '(not written)')}\n")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
