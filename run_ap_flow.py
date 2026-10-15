"""AP flow: drain vendor-invoice events -> match -> tax engine -> GL -> subledger -> mart.

Requires the engine: python run_engine.py

Usage: python run_ap_flow.py
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from src.config import load_config
from src.integrations.ap_flow import run_ap_flow
from src.integrations.engine_client import EngineClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Vendor invoice -> tax engine -> GL flow")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    parser.add_argument("--backoff", type=float, default=0.2)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()

    cfg = load_config(args.root)
    client = EngineClient(base_url=cfg["engine"]["base_url"])
    try:
        client.health()
    except httpx.HTTPError:
        print(f"ERROR: tax engine not reachable at {cfg['engine']['base_url']}.")
        print("Start it first:  python run_engine.py")
        return 2

    summary = run_ap_flow(
        cfg=cfg, root=args.root, engine=client,
        max_attempts=args.max_attempts, backoff_s=args.backoff,
    )
    print("AP flow complete")
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    sys.exit(main())
