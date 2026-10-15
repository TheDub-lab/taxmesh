"""Go-live readiness gate for the AP migration. Exit code 0 when READY.

Usage:
  python run_go_live.py
  python run_go_live.py --inject legacy_drift   # should report NOT_READY
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from src.config import load_config
from src.controls.go_live import run_go_live
from src.integrations.engine_client import EngineClient


def main() -> int:
    parser = argparse.ArgumentParser(description="AP go-live readiness checklist")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    parser.add_argument("--inject", default="", help="comma-separated faults: legacy_drift")
    parser.add_argument("--json", action="store_true", help="print raw JSON only")
    args = parser.parse_args()

    inject = [s.strip() for s in args.inject.split(",") if s.strip()]
    cfg = load_config(args.root)
    client = EngineClient(base_url=cfg["engine"]["base_url"])
    try:
        client.health()
    except httpx.HTTPError:
        print(f"ERROR: tax engine not reachable at {cfg['engine']['base_url']}.")
        print("Start it first:  python run_engine.py")
        return 2

    result = run_go_live(cfg=cfg, root=args.root, engine=client, inject=inject)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "READY" else 1

    print(f"\nGO-LIVE CHECKLIST {result['runId']}  —  {result['status']}")
    print("-" * 78)
    for gate in result["gates"]:
        marker = "PASS" if gate["status"] == "PASS" else "FAIL"
        observed = f"  (observed {gate['observed']})" if "observed" in gate else ""
        print(f"[{marker}] {gate['id']:<40} {gate['description']}{observed}")
    if result["blockedInvoices"]:
        print("-" * 78)
        print(f"blocked invoices: {len(result['blockedInvoices'])}")
    print(f"\nreport: {result.get('outputPath', '(not written)')}\n")
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    sys.exit(main())
