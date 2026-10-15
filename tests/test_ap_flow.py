"""AP flow: match -> tax -> GL -> subledger -> mart, with idempotency and blocks."""

from __future__ import annotations

from src.controls.ap_controls import run_ap_controls
from src.integrations.ap_flow import run_ap_flow
from src.integrations.flow import run_flow
from src.sources import ap, erp, tax_mart
from src.sources.seed import run_seed
from src.sources.seed_ap import run_seed_ap


def test_ap_seed_reset_clears_shared_ar_state(ap_workspace, ap_engine):
    # Simulate a prior AR run that left SALE transactions and GL entries behind.
    run_seed(cfg=ap_workspace.cfg, root=ap_workspace.root)
    run_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert any(t["docType"] == "SALE" for t in ap_engine.transactions())
    assert erp.load_journal(ap_workspace.paths)

    # An AP-only seed with reset=True must clear that shared state.
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root, reset=True)
    assert ap_engine.transactions() == []
    assert erp.load_journal(ap_workspace.paths) == []

    run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    result = run_ap_controls(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert result["status"] == "PASS"


def test_clean_ap_flow_posts_every_voucher(ap_workspace, ap_engine):
    summary = run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root)
    n = summary["vendorInvoices"]

    result = run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert result["status"] == "COMPLETED"
    assert len(result["posted"]) == n
    assert result["blocked"] == []
    assert result["dlq"] == []
    assert result["pendingRemaining"] == 0

    vouchers = ap.load_vouchers(ap_workspace.paths)
    assert all(v["status"] == "posted" for v in vouchers)
    assert len(ap.load_subledger(ap_workspace.paths)) == n
    assert len(tax_mart.vendor_invoice_rows(ap_workspace.paths)) == n

    txns = ap_engine.transactions()
    assert len(txns) == n
    assert all(t["docType"] == "PURCHASE" for t in txns)

    ap_entries = [e for e in erp.load_journal(ap_workspace.paths) if e.get("voucherId")]
    assert len(ap_entries) == n
    for entry in ap_entries:
        debit = round(sum(l["debit"] for l in entry["lines"]), 2)
        credit = round(sum(l["credit"] for l in entry["lines"]), 2)
        assert debit == credit


def test_ap_flow_is_idempotent_on_rerun(ap_workspace, ap_engine):
    summary = run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root)
    n = summary["vendorInvoices"]

    first = run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert len(first["posted"]) == n
    assert first["skipped"] == []
    txns_after_first = len(ap_engine.transactions())

    # simulate at-least-once redelivery: put every event back to pending
    for event in ap.load_outbox(ap_workspace.paths):
        ap.mark_event(ap_workspace.paths, event["eventId"], "pending")

    second = run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert second["posted"] == []
    assert len(second["skipped"]) == n            # idempotency store caught them
    assert len(ap_engine.transactions()) == txns_after_first  # no duplicate commits
    assert len(ap.load_subledger(ap_workspace.paths)) == n
    assert len([e for e in erp.load_journal(ap_workspace.paths) if e.get("voucherId")]) == n


def test_blocked_vouchers_are_never_posted(ap_workspace, ap_engine):
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root,
                inject=["price_variance", "no_po", "duplicate_invoice"])

    result = run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert result["status"] == "COMPLETED"
    codes = {b["code"] for b in result["blocked"]}
    assert codes == {"PRICE_VARIANCE", "NO_PO", "DUPLICATE"}
    assert len(result["blocked"]) == 3

    vouchers = {v["voucherId"]: v for v in ap.load_vouchers(ap_workspace.paths)}
    for block in result["blocked"]:
        assert vouchers[block["voucherId"]]["status"] == "blocked"
        assert block["voucherId"] not in {e["voucherId"] for e in
                                          erp.load_journal(ap_workspace.paths)
                                          if e.get("voucherId")}

    posted = [v for v in vouchers.values() if v["status"] == "posted"]
    assert len(ap.load_subledger(ap_workspace.paths)) == len(posted)
    assert len(tax_mart.vendor_invoice_rows(ap_workspace.paths)) == len(posted)


def test_use_tax_is_self_assessed_when_vendor_charges_none(ap_workspace, ap_engine):
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root)
    run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)

    vouchers = ap.load_vouchers(ap_workspace.paths)
    with_self = [v for v in vouchers if v["tax"]["selfAssessedTaxTotal"] > 0]
    assert with_self, "expected at least one purchase with self-assessed use tax"
    for voucher in with_self:
        for line in voucher["tax"]["lines"]:
            if line["selfAssessedTax"] > 0:
                assert line["vendorTax"] == 0.0  # self-assessed only where vendor charged none


def test_ap_controls_pass_after_clean_flow(ap_workspace, ap_engine):
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root)
    run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)

    result = run_ap_controls(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    assert result["status"] == "PASS"
    assert all(c["status"] == "PASS" for c in result["controls"])
    assert all(r["status"] == "PASS" for r in result["byJurisdiction"])
    assert result["exceptions"]["parseErrors"] == []

    by_name = {c["name"]: c for c in result["controls"]}
    assert by_name["money_recoverable_vs_gl_input_tax"]["left"] > 0
    assert by_name["money_selfassessed_vs_gl_use_tax"]["left"] > 0


def test_blocked_invoice_is_named_but_controls_still_reconcile(ap_workspace, ap_engine):
    run_seed_ap(cfg=ap_workspace.cfg, root=ap_workspace.root,
                inject=["price_variance", "no_po", "duplicate_invoice"])
    run_ap_flow(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)

    result = run_ap_controls(cfg=ap_workspace.cfg, root=ap_workspace.root, engine=ap_engine)
    # integrity controls still tie: blocked invoices never reached GL/engine/mart
    assert result["status"] == "PASS"
    by_name = {c["name"]: c for c in result["controls"]}
    assert by_name["count_posted_subledger_vs_gl"]["status"] == "PASS"
    assert by_name["blocked_vouchers_not_posted"]["status"] == "PASS"
    assert result["counts"]["blocked"] == 3

    codes = {b["code"] for b in result["exceptions"]["blockedInvoices"]}
    assert codes == {"PRICE_VARIANCE", "NO_PO", "DUPLICATE"}
