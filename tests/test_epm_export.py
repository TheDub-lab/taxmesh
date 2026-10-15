"""EPM landing export: determinism, row counts, and the combined tax extract."""

from __future__ import annotations

import csv
import io

from src.exports.epm import EXTRACTS, run_epm_export
from src.integrations.ap_flow import run_ap_flow
from src.integrations.flow import run_flow
from src.sources import tax_mart
from src.sources.seed import run_seed
from src.sources.seed_ap import run_seed_ap


def _populate(ws, engine):
    ws.cfg["seed"]["invoices"] = 8
    run_seed(cfg=ws.cfg, root=ws.root)
    run_flow(cfg=ws.cfg, root=ws.root, engine=engine)
    run_seed_ap(cfg=ws.cfg, root=ws.root)
    run_ap_flow(cfg=ws.cfg, root=ws.root, engine=engine)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_export_writes_all_extracts(ap_workspace, ap_engine):
    _populate(ap_workspace, ap_engine)
    result = run_epm_export(cfg=ap_workspace.cfg, root=ap_workspace.root)

    written = {f["name"] for f in result["extracts"]}
    assert written == {f"{name}.csv" for name in EXTRACTS}
    for name in EXTRACTS:
        assert (ap_workspace.paths.epm / f"{name}.csv").exists()


def test_manifest_rows_match_the_mart(ap_workspace, ap_engine):
    _populate(ap_workspace, ap_engine)
    result = run_epm_export(cfg=ap_workspace.cfg, root=ap_workspace.root)

    rows = {f["name"]: f["rows"] for f in result["extracts"]}
    assert rows["fct_invoices.csv"] == len(tax_mart.invoice_rows(ap_workspace.paths))
    assert rows["fct_vendor_invoices.csv"] == len(
        tax_mart.vendor_invoice_rows(ap_workspace.paths))
    assert rows["dim_jurisdictions.csv"] == len(
        tax_mart.jurisdiction_rows(ap_workspace.paths))


def test_export_is_deterministic(ap_workspace, ap_engine):
    _populate(ap_workspace, ap_engine)
    first = run_epm_export(cfg=ap_workspace.cfg, root=ap_workspace.root)
    before = {f["name"]: _read(ap_workspace.paths.epm / f["name"]) for f in first["extracts"]}

    second = run_epm_export(cfg=ap_workspace.cfg, root=ap_workspace.root)
    after = {f["name"]: _read(ap_workspace.paths.epm / f["name"]) for f in second["extracts"]}

    assert before == after
    assert {f["name"]: f["sha256"] for f in first["extracts"]} == \
           {f["name"]: f["sha256"] for f in second["extracts"]}


def test_jurisdiction_tax_combines_revenue_and_ap(ap_workspace, ap_engine):
    _populate(ap_workspace, ap_engine)
    run_epm_export(cfg=ap_workspace.cfg, root=ap_workspace.root)

    text = _read(ap_workspace.paths.epm / "jurisdiction_tax.csv")
    rows = list(csv.DictReader(io.StringIO(text)))
    ar = tax_mart.tax_line_totals_by_jurisdiction(ap_workspace.paths)
    ap = tax_mart.self_assessed_tax_by_jurisdiction(ap_workspace.paths)

    assert {r["jurisdiction_id"] for r in rows} == set(ar) | set(ap)
    for row in rows:
        jid = row["jurisdiction_id"]
        assert float(row["ar_sales_tax"]) == round(ar.get(jid, 0.0), 2)
        assert float(row["ap_self_assessed_use_tax"]) == round(ap.get(jid, 0.0), 2)
        assert float(row["total_tax"]) == round(ar.get(jid, 0.0) + ap.get(jid, 0.0), 2)
