"""Run AP controls / tie-outs. Exit code: 0 when every control passes.

Usage: python run_ap_controls.py
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from src.config import load_config
from src.controls.ap_controls import run_ap_controls
from src.integrations.engine_client import EngineClient


def _fmt(value) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="AP control totals and tie-outs")
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

    result = run_ap_controls(cfg=cfg, root=args.root, engine=client)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "PASS" else 1

    counts = result["counts"]
    print(f"\nAP CONTROL RUN {result['runId']}  —  OVERALL: {result['status']}")
    print(f"vouchers {counts['vouchers']}  posted {counts['posted']}  "
          f"blocked {counts['blocked']}  pending {counts['pending']}")
    print("-" * 78)
    for c in result["controls"]:
        marker = "PASS" if c["status"] == "PASS" else "FAIL"
        print(f"[{marker}] {c['name']:<40} {_fmt(c['left'])} vs {_fmt(c['right'])}"
              + (f"  delta {c['delta']}" if c["delta"] else ""))
    print("-" * 78)
    for row in result["byJurisdiction"]:
        marker = "PASS" if row["status"] == "PASS" else "FAIL"
        print(f"[{marker}] jurisdiction {row['jurisdictionId']:<12} "
              f"engine {row['engineSelfAssessed']:>10,.2f}  gl {row['glUseTax']:>10,.2f}")
    exc = result["exceptions"]
    if exc["blockedInvoices"] or exc["dlqEvents"] or exc["pendingEvents"]:
        print("-" * 78)
        print("EXCEPTIONS DETECTED BY CONTROLS:")
        if exc["blockedInvoices"]:
            named = ", ".join(f"{b['voucherId']}({b['code']})" for b in exc["blockedInvoices"][:10])
            print(f"  blocked invoices ({len(exc['blockedInvoices'])}): {named}")
        if exc["dlqEvents"]:
            print(f"  dead-lettered events: {', '.join(exc['dlqEvents'])}")
        if exc["pendingEvents"]:
            print(f"  unprocessed events: {', '.join(exc['pendingEvents'])}")
    print(f"\nreport: {result.get('outputPath', '(not written)')}\n")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
