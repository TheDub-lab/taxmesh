"""Three-way match: PO vs goods receipt vs vendor invoice."""

from __future__ import annotations

from src.sources import ap, procurement


def _seed_master(paths):
    procurement.save_suppliers(paths, [
        {"supplierCode": "SUP-001", "name": "Cascade", "taxId": "T1",
         "remitToZip": "94105", "countryCode": "US"},
    ])
    procurement.save_purchase_orders(paths, [{
        "poId": "PO-00001", "supplierCode": "SUP-001", "orderDate": "2026-09-01",
        "deliveryZip": "94105", "currency": "USD", "status": "open",
        "lines": [{"lineId": "1", "sku": "SKU-HARDWARE", "description": "Printer",
                   "taxCode": "HARDWARE", "quantity": 3, "unitPrice": 500.0,
                   "amount": 1500.0}],
    }])
    procurement.save_receipts(paths, [{
        "receiptId": "GR-00001", "poId": "PO-00001", "receivedDate": "2026-09-03",
        "lines": [{"lineId": "1", "quantityReceived": 3}],
    }])


def _voucher(**over):
    voucher = {
        "voucherId": "VINV-00001", "supplierInvoiceNumber": "SUP-0001",
        "supplierCode": "SUP-001", "poId": "PO-00001", "invoiceDate": "2026-09-05",
        "deliveryZip": "94105", "subtotal": 1500.0,
        "lines": [{"lineId": "1", "poLineId": "1", "taxCode": "HARDWARE",
                   "quantity": 3, "unitPrice": 500.0, "amount": 1500.0, "taxCharged": 0.0}],
    }
    voucher.update(over)
    return voucher


def _cfg():
    return {"ap": {"three_way_match": {"price_tolerance_cents": 0, "quantity_tolerance": 0}}}


def test_clean_three_way_match(ap_workspace):
    paths = ap_workspace.paths
    _seed_master(paths)
    voucher = _voucher()
    ap.save_vouchers(paths, [voucher])

    match = ap.match_voucher(_cfg(), paths, voucher)
    assert match["status"] == "MATCHED"
    assert match["variances"] == []


def test_price_variance_is_caught(ap_workspace):
    paths = ap_workspace.paths
    _seed_master(paths)
    voucher = _voucher()
    voucher["lines"][0]["unitPrice"] = 512.50
    ap.save_vouchers(paths, [voucher])

    match = ap.match_voucher(_cfg(), paths, voucher)
    assert match["status"] == "PRICE_VARIANCE"
    assert "512.50" in match["variances"][0]["detail"]


def test_price_within_tolerance_passes(ap_workspace):
    paths = ap_workspace.paths
    _seed_master(paths)
    voucher = _voucher()
    voucher["lines"][0]["unitPrice"] = 500.05
    ap.save_vouchers(paths, [voucher])

    cfg = {"ap": {"three_way_match": {"price_tolerance_cents": 10, "quantity_tolerance": 0}}}
    assert ap.match_voucher(cfg, paths, voucher)["status"] == "MATCHED"


def test_quantity_variance_against_receipt(ap_workspace):
    paths = ap_workspace.paths
    _seed_master(paths)
    voucher = _voucher()
    voucher["lines"][0]["quantity"] = 5
    ap.save_vouchers(paths, [voucher])

    match = ap.match_voucher(_cfg(), paths, voucher)
    assert match["status"] == "QUANTITY_VARIANCE"


def test_invoice_without_po_is_blocked(ap_workspace):
    paths = ap_workspace.paths
    _seed_master(paths)
    voucher = _voucher(poId=None)
    ap.save_vouchers(paths, [voucher])

    assert ap.match_voucher(_cfg(), paths, voucher)["status"] == "NO_PO"


def test_missing_po_is_reported(ap_workspace):
    paths = ap_workspace.paths
    _seed_master(paths)
    voucher = _voucher(poId="PO-99999")
    ap.save_vouchers(paths, [voucher])

    assert ap.match_voucher(_cfg(), paths, voucher)["status"] == "PO_NOT_FOUND"


def test_duplicate_flags_only_the_later_voucher(ap_workspace):
    paths = ap_workspace.paths
    _seed_master(paths)
    first = _voucher(voucherId="VINV-00001", supplierInvoiceNumber="SUP-0001")
    second = _voucher(voucherId="VINV-00002", supplierInvoiceNumber="SUP-0001")
    ap.save_vouchers(paths, [first, second])

    assert ap.match_voucher(_cfg(), paths, first)["status"] == "MATCHED"
    match = ap.match_voucher(_cfg(), paths, second)
    assert match["status"] == "DUPLICATE"
    assert match["variances"][0]["duplicateOf"] == "VINV-00001"
