"""Run the Atlas tax engine as a real HTTP service.

Usage: python run_engine.py   (then GET http://127.0.0.1:8077/v2/health)
"""

from __future__ import annotations

import argparse

import uvicorn

from src.config import load_config
from src.engine.app import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Atlas tax engine service")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    args = parser.parse_args()

    cfg = load_config(args.root)
    app = create_app(cfg=cfg, root=args.root)
    host, port = cfg["engine"]["host"], int(cfg["engine"]["port"])
    print(f"atlas-tax-engine listening on http://{host}:{port}  (docs at /docs)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
