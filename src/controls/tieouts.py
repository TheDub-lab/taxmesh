"""Source-to-target tie-outs: control totals across billing, engine, GL, and mart.

Each control compares two independently extracted views of the same truth.
Anything non-zero (or non-equal) is an exception with a named variance, which
Phase 3/4 route into the exception queue and evidence bundle.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Paths, get_paths, load_config
from ..integrations.engine_client import EngineClient
from ..sources import billing, erp, tax_mart
from ..util import atomic_write_json, new_run_id, now_iso, short_hash

TOL_CENTS_DEFAULT = 0


def _money(value) -> float:
    return round(float(value or 0), 2)


def _add_control(controls: list, name: str, left_label: str, left, right_label: str,
                 right, kind: str = "count", tol: int = TOL_CENTS_DEFAULT):
    if kind == "money":
        left, right = _money(left), _money(right)
        delta = round(left - right, 2)
        status = "PASS" if abs(delta) <= tol / 100.0 else "FAIL"
    elif kind == "hash":
        delta = None
        status = "PASS" if left == right else "FAIL"
    else:
        left, right = int(left), int(right)
        delta = left - right
        status = "PASS" if delta == 0 else "FAIL"
    controls.append({
        "name": name, "kind": kind, "leftLabel": left_label, "left": left,
        "rightLabel": right_label, "right": right, "delta": delta, "status": status,
    })
    return status == "PASS"


def run_tieouts(
    cfg: dict | None = None,
    root=None,
    engine: EngineClient | None = None,
    write: bool = True,
) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()
    tol = int(cfg.get("controls", {}).get("monetary_tolerance_cents", TOL_CENTS_DEFAULT))

    own_engine = engine is None
    if own_engine:
        engine = EngineClient(base_url=cfg["engine"]["base_url"])

    try:
        engine_txns = engine.transactions()
    finally:
        if own_engine:
            engine.close()

    invoices = [i for i in billing.load_invoices(paths) if i["status"] == "finalized"]
    outbox = billing.load_outbox(paths)
    journal = erp.load_journal(paths)
    mart_rows = tax_mart.invoice_rows(paths)
    accounts = cfg["gl_accounts"]

    b_ids = sorted(i["invoiceId"] for i in invoices)
    e_ids = sorted(t["docId"] for t in engine_txns)
    g_ids = sorted(e["invoiceId"] for e in journal)
    m_ids = sorted(r["invoice_id"] for r in mart_rows)

    missing_in_engine = [i for i in b_ids if i not in set(e_ids)]
    extra_in_engine = [i for i in e_ids if i not in set(b_ids)]

    b_subtotal = _money(sum(i["subtotal"] for i in invoices))
    e_subtotal = _money(sum(t["subtotal"] for t in engine_txns))
    e_tax = _money(sum(t["totalTax"] for t in engine_txns))
    e_withholding = _money(sum(t["withholding"]["amount"] for t in engine_txns))
    billed_tax = _money(sum((i["tax"]["totalTax"] if i.get("tax") else 0.0) for i in invoices))

    gl_revenue = _money(sum(l["credit"] for e in journal for l in e["lines"]
                            if l["account"] == accounts["REVENUE"]))
    gl_tax = _money(sum(l["credit"] for e in journal for l in e["lines"]
                        if l["account"] == accounts["SALES_TAX_PAYABLE"]))
    gl_withholding = _money(sum(l["credit"] for e in journal for l in e["lines"]
                                if l["account"] == accounts["WITHHOLDING_PAYABLE"]))
    gl_tax_by_juris: dict[str, float] = {}
    for e in journal:
        for l in e["lines"]:
            if l["account"] == accounts["SALES_TAX_PAYABLE"] and l["jurisdictionId"]:
                jid = l["jurisdictionId"]
                gl_tax_by_juris[jid] = _money(gl_tax_by_juris.get(jid, 0) + l["credit"])

    engine_tax_by_juris: dict[str, float] = {}
    for t in engine_txns:
        for line in t["lines"]:
            for row in line["jurisdictions"]:
                jid = row["jurisdictionId"]
                engine_tax_by_juris[jid] = _money(
                    engine_tax_by_juris.get(jid, 0) + row["tax"]
                )

    controls: list[dict] = []
    _add_control(controls, "count_billing_vs_engine", "billing.finalized", len(b_ids),
                 "engine.committed", len(e_ids))
    _add_control(controls, "count_billing_vs_gl", "billing.finalized", len(b_ids),
                 "gl.invoices_posted", len(g_ids))
    _add_control(controls, "count_billing_vs_mart", "billing.finalized", len(b_ids),
                 "mart.fct_invoices", len(m_ids))
    _add_control(controls, "money_subtotal_billing_vs_engine", "billing.subtotal", b_subtotal,
                 "engine.subtotal", e_subtotal, kind="money", tol=tol)
    _add_control(controls, "money_subtotal_billing_vs_gl", "billing.subtotal", b_subtotal,
                 "gl.revenue_credits", gl_revenue, kind="money", tol=tol)
    _add_control(controls, "money_tax_engine_vs_billing", "engine.totalTax", e_tax,
                 "billing.tax_recorded", billed_tax, kind="money", tol=tol)
    _add_control(controls, "money_tax_engine_vs_gl", "engine.totalTax", e_tax,
                 "gl.tax_payable_credits", gl_tax, kind="money", tol=tol)
    _add_control(controls, "money_withholding_engine_vs_gl", "engine.withholding", e_withholding,
                 "gl.withholding_credits", gl_withholding, kind="money", tol=tol)
    _add_control(controls, "hash_invoice_ids_billing_vs_engine", "billing.id_hash",
                 short_hash(b_ids), "engine.id_hash", short_hash(e_ids), kind="hash")

    by_jurisdiction = []
    for jid in sorted(set(engine_tax_by_juris) | set(gl_tax_by_juris)):
        left = _money(engine_tax_by_juris.get(jid, 0))
        right = _money(gl_tax_by_juris.get(jid, 0))
        by_jurisdiction.append({
            "jurisdictionId": jid, "engineTax": left, "glTax": right,
            "delta": round(left - right, 2),
            "status": "PASS" if left == right else "FAIL",
        })

    dlq_events = [e for e in outbox if e["status"] == "dlq"]
    pending_events = [e for e in outbox if e["status"] == "pending"]
    overall = "PASS"
    if any(c["status"] == "FAIL" for c in controls) or any(
        r["status"] == "FAIL" for r in by_jurisdiction
    ):
        overall = "FAIL"
    if dlq_events or pending_events:
        overall = "FAIL"

    result = {
        "runId": new_run_id("tieouts"),
        "generatedAt": now_iso(),
        "status": overall,
        "controls": controls,
        "byJurisdiction": by_jurisdiction,
        "exceptions": {
            "missingInEngine": missing_in_engine,
            "extraInEngine": extra_in_engine,
            "dlqEvents": [e["eventId"] for e in dlq_events],
            "pendingEvents": [e["eventId"] for e in pending_events],
        },
        "extractHashes": {
            "billing": short_hash(b_ids),
            "engine": short_hash(e_ids),
            "gl": short_hash(g_ids),
            "mart": short_hash(m_ids),
        },
    }

    if write:
        paths.tieouts.mkdir(parents=True, exist_ok=True)
        out: Path = paths.tieouts / f"{result['runId']}.json"
        atomic_write_json(out, result)
        result["outputPath"] = str(out)
    return result
