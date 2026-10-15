"""AP controls: source-to-target tie-outs for the vendor-invoice stream.

Same shape as the AR controls: each control compares two independently
extracted views. AP adds two control families the AR side does not have —
match-exception accounting (blocked vouchers must never post) and e-invoicing
parse coverage.
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_paths, load_config
from ..integrations.engine_client import EngineClient
from ..sources import ap, erp, tax_mart
from ..util import atomic_write_json, new_run_id, now_iso, short_hash
from .tieouts import _add_control, _money

TOL_CENTS_DEFAULT = 0


def run_ap_controls(
    cfg: dict | None = None,
    root=None,
    engine: EngineClient | None = None,
    write: bool = True,
) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()
    tol = int(cfg.get("controls", {}).get("monetary_tolerance_cents", TOL_CENTS_DEFAULT))
    accounts = cfg["gl_accounts"]

    own_engine = engine is None
    if own_engine:
        engine = EngineClient(base_url=cfg["engine"]["base_url"])
    try:
        engine_txns = [t for t in engine.transactions()
                       if t.get("docType") == "PURCHASE"]
    finally:
        if own_engine:
            engine.close()

    vouchers = ap.load_vouchers(paths)
    outbox = ap.load_outbox(paths)
    subledger = ap.load_subledger(paths)
    journal = [e for e in erp.load_journal(paths) if e.get("voucherId")]
    mart_rows = tax_mart.vendor_invoice_rows(paths)

    posted = [v for v in vouchers if v["status"] == "posted"]
    blocked = [v for v in vouchers if v["status"] == "blocked"]
    pending = [v for v in vouchers if v["status"] == "received"]

    posted_ids = sorted(v["voucherId"] for v in posted)
    engine_ids = sorted(t["docId"] for t in engine_txns)
    gl_ids = sorted(e["voucherId"] for e in journal)
    sub_ids = sorted(r["voucherId"] for r in subledger)
    mart_ids = sorted(r["voucher_id"] for r in mart_rows)

    sub_total = _money(sum(r["amount"] for r in subledger))
    gl_ap_credit = _money(sum(l["credit"] for e in journal for l in e["lines"]
                              if l["account"] == accounts["AP"]))
    gl_input_tax = _money(sum(l["debit"] for e in journal for l in e["lines"]
                              if l["account"] == accounts["INPUT_TAX_RECOVERABLE"]))
    gl_use_tax = _money(sum(l["credit"] for e in journal for l in e["lines"]
                            if l["account"] == accounts["USE_TAX_PAYABLE"]))

    recoverable = _money(sum(v["tax"]["recoverableTaxTotal"]
                             for v in posted if v.get("tax")))
    self_assessed = _money(sum(v["tax"]["selfAssessedTaxTotal"]
                               for v in posted if v.get("tax")))

    gl_use_tax_by_juris: dict[str, float] = {}
    for e in journal:
        for l in e["lines"]:
            if l["account"] == accounts["USE_TAX_PAYABLE"] and l["jurisdictionId"]:
                jid = l["jurisdictionId"]
                gl_use_tax_by_juris[jid] = _money(gl_use_tax_by_juris.get(jid, 0) + l["credit"])

    engine_self_by_juris: dict[str, float] = {}
    for t in engine_txns:
        doc_id = t["docId"]
        voucher = next((v for v in posted if v["voucherId"] == doc_id), None)
        if not voucher or not voucher.get("tax"):
            continue
        for line in voucher["tax"]["lines"]:
            if line["selfAssessedTax"] <= 0:
                continue
            for row in line["jurisdictions"]:
                jid = row["jurisdictionId"]
                engine_self_by_juris[jid] = _money(
                    engine_self_by_juris.get(jid, 0) + row["tax"])

    coverage = ap.ingest_einvoice_inbox(paths)  # re-parse to report coverage honestly
    inbox_files = len(sorted((paths.einvoicing / "inbox").glob("*.xml"))) \
        if (paths.einvoicing / "inbox").exists() else 0

    gl_id_set = set(gl_ids)
    blocked_but_posted = sorted(
        v["voucherId"] for v in blocked if v["voucherId"] in gl_id_set
    )

    controls: list[dict] = []
    _add_control(controls, "count_posted_subledger_vs_gl", "ap.subledger", len(sub_ids),
                 "gl.ap_entries", len(gl_ids))
    _add_control(controls, "count_posted_vs_mart", "ap.posted", len(posted_ids),
                 "mart.fct_vendor_invoices", len(mart_ids))
    _add_control(controls, "count_posted_vs_engine", "ap.posted", len(posted_ids),
                 "engine.purchase_txns", len(engine_ids))
    _add_control(controls, "money_subledger_vs_gl_ap_credit", "ap.subledger_total", sub_total,
                 "gl.ap_credit", gl_ap_credit, kind="money", tol=tol)
    _add_control(controls, "money_recoverable_vs_gl_input_tax", "ap.recoverable_tax",
                 recoverable, "gl.input_tax_debits", gl_input_tax, kind="money", tol=tol)
    _add_control(controls, "money_selfassessed_vs_gl_use_tax", "ap.self_assessed_tax",
                 self_assessed, "gl.use_tax_credits", gl_use_tax, kind="money", tol=tol)
    _add_control(controls, "hash_voucher_ids_subledger_vs_gl", "ap.subledger_ids",
                 short_hash(sub_ids), "gl.voucher_ids", short_hash(gl_ids), kind="hash")
    _add_control(controls, "einvoice_parse_coverage", "einvoicing.inbox_files",
                 inbox_files, "einvoicing.parsed", len(coverage["parsed"]))
    _add_control(controls, "blocked_vouchers_not_posted", "blocked_but_posted",
                 len(blocked_but_posted), "expected", 0)

    by_jurisdiction = []
    for jid in sorted(set(engine_self_by_juris) | set(gl_use_tax_by_juris)):
        left = _money(engine_self_by_juris.get(jid, 0))
        right = _money(gl_use_tax_by_juris.get(jid, 0))
        by_jurisdiction.append({
            "jurisdictionId": jid, "engineSelfAssessed": left, "glUseTax": right,
            "delta": round(left - right, 2),
            "status": "PASS" if left == right else "FAIL",
        })

    dlq_events = [e for e in outbox if e["status"] == "dlq"]
    pending_events = [e for e in outbox if e["status"] == "pending"]
    unmatched = [v["voucherId"] for v in vouchers
                 if (v.get("match") or {}).get("status") not in (None, "MATCHED")]

    overall = "PASS"
    if any(c["status"] == "FAIL" for c in controls) or any(
        r["status"] == "FAIL" for r in by_jurisdiction
    ):
        overall = "FAIL"
    if dlq_events or pending_events or coverage["errors"]:
        overall = "FAIL"

    result = {
        "runId": new_run_id("ap_tieouts"),
        "generatedAt": now_iso(),
        "status": overall,
        "controls": controls,
        "byJurisdiction": by_jurisdiction,
        "exceptions": {
            "blockedInvoices": [{"voucherId": v["voucherId"],
                                 "code": (v.get("match") or {}).get("status")}
                                for v in blocked],
            "unmatched": unmatched,
            "blockedButPosted": blocked_but_posted,
            "dlqEvents": [e["eventId"] for e in dlq_events],
            "pendingEvents": [e["eventId"] for e in pending_events],
            "parseErrors": coverage["errors"],
        },
        "counts": {
            "vouchers": len(vouchers),
            "posted": len(posted_ids),
            "blocked": len(blocked),
            "pending": len(pending),
        },
        "extractHashes": {
            "subledger": short_hash(sub_ids),
            "gl": short_hash(gl_ids),
            "engine": short_hash(engine_ids),
            "mart": short_hash(mart_ids),
        },
    }

    if write:
        paths.ap_tieouts.mkdir(parents=True, exist_ok=True)
        out: Path = paths.ap_tieouts / f"{result['runId']}.json"
        atomic_write_json(out, result)
        result["outputPath"] = str(out)
    return result
