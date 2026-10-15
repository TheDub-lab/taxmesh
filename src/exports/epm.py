"""EPM landing export.

The tax mart is the system of record for tax facts; Planwell EPM consumes flat
extracts rather than querying the mart directly (the classic pattern). This
module builds deterministic CSV extracts from the mart and writes them, with a
manifest that records the row count and content hash of every file, so the
hand-off itself is auditable.

Determinism matters here: rows are sorted, line endings are fixed, and volatile
columns (load timestamps) are excluded, so re-exporting an unchanged mart yields
byte-identical files.
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

from ..config import Paths, get_paths, load_config
from ..sources import tax_mart
from ..util import atomic_write_json, atomic_write_text, new_run_id, now_iso

# name -> column order. Volatile columns (loaded_at) are deliberately omitted.
EXTRACTS = {
    "dim_jurisdictions": [
        "jurisdiction_id", "state_code", "state_name", "county_name", "city_name",
        "total_rate",
    ],
    "fct_invoices": [
        "invoice_id", "customer_code", "issue_date", "status", "jurisdiction_id",
        "subtotal", "tax_total", "withholding_total", "engine_txn_id",
    ],
    "fct_vendor_invoices": [
        "voucher_id", "supplier_code", "supplier_invoice_number", "po_id",
        "invoice_date", "status", "delivery_zip", "match_status", "subtotal",
        "vendor_tax_total", "self_assessed_tax_total", "recoverable_tax_total",
        "nonrecoverable_tax_total", "engine_txn_id", "source",
    ],
    "jurisdiction_tax": [
        "jurisdiction_id", "ar_sales_tax", "ap_self_assessed_use_tax", "total_tax",
    ],
}


def _ar_invoice_rows(paths: Paths) -> list[dict]:
    return tax_mart.invoice_rows(paths)


def _ap_invoice_rows(paths: Paths) -> list[dict]:
    return tax_mart.vendor_invoice_rows(paths)


def _jurisdiction_tax_rows(paths: Paths) -> list[dict]:
    ar = tax_mart.tax_line_totals_by_jurisdiction(paths)
    ap = tax_mart.self_assessed_tax_by_jurisdiction(paths)
    rows = []
    for jid in sorted(set(ar) | set(ap)):
        ar_amt = round(ar.get(jid, 0.0), 2)
        ap_amt = round(ap.get(jid, 0.0), 2)
        rows.append({
            "jurisdiction_id": jid,
            "ar_sales_tax": ar_amt,
            "ap_self_assessed_use_tax": ap_amt,
            "total_tax": round(ar_amt + ap_amt, 2),
        })
    return rows


def build_extracts(cfg: dict, paths: Paths) -> dict[str, list[dict]]:
    """Build every extract as a list of dicts keyed by the EXTRACTS columns."""
    return {
        "dim_jurisdictions": tax_mart.jurisdiction_rows(paths),
        "fct_invoices": _ar_invoice_rows(paths),
        "fct_vendor_invoices": _ap_invoice_rows(paths),
        "jurisdiction_tax": _jurisdiction_tax_rows(paths),
    }


def _render_csv(columns: list[str], rows: list[dict]) -> bytes:
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(col, "") for col in columns])
    return buf.getvalue().encode("utf-8")


def run_epm_export(cfg: dict | None = None, root=None, write: bool = True) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()
    paths.epm.mkdir(parents=True, exist_ok=True)

    extracts = build_extracts(cfg, paths)

    files: list[dict] = []
    for name, columns in EXTRACTS.items():
        rows = extracts[name]
        payload = _render_csv(columns, rows)
        entry = {
            "name": f"{name}.csv",
            "columns": columns,
            "rows": len(rows),
            "sha256": hashlib.sha256(payload).hexdigest()[:16],
        }
        if write:
            target = paths.epm / f"{name}.csv"
            atomic_write_text(target, payload.decode("utf-8"))
        files.append(entry)

    manifest = {
        "runId": new_run_id("epm_export"),
        "generatedAt": now_iso(),
        "system": "Planwell EPM",
        "extracts": files,
        "totalRows": sum(f["rows"] for f in files),
    }
    if write:
        atomic_write_json(paths.epm / "epm_manifest.json", manifest)
        manifest["outputPath"] = str(paths.epm)
    return manifest
