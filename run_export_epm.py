"""Export tax-mart facts to the Planwell EPM landing zone.

Usage: python run_export_epm.py
"""

from __future__ import annotations

import argparse
import json
import sys

from src.config import load_config
from src.exports.epm import run_epm_export


def main() -> int:
    parser = argparse.ArgumentParser(description="EPM landing-zone export")
    parser.add_argument("--root", default=None, help="workspace root (defaults to repo root)")
    parser.add_argument("--json", action="store_true", help="print raw JSON only")
    args = parser.parse_args()

    cfg = load_config(args.root)
    result = run_epm_export(cfg=cfg, root=args.root)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print(f"\nEPM EXPORT {result['runId']}  ->  {result.get('outputPath', '')}")
    print("-" * 78)
    for f in result["extracts"]:
        print(f"  {f['name']:<28} {f['rows']:>6} rows   sha256 {f['sha256']}")
    print(f"  total rows: {result['totalRows']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
