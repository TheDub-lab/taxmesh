"""Payline AP synthetic source: vendor invoices, event outbox, AP subledger.

Also owns two pieces of AP domain logic that are pure enough to unit-test on
their own: three-way match (PO vs goods receipt vs invoice) and the input-tax
split (vendor-charged tax vs self-assessed use tax, recoverable vs not).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Paths
from ..util import atomic_write_json, now_iso
from . import einvoicing


def _path(paths: Paths, name: str) -> Path:
    return paths.ap / name


def load_vouchers(paths: Paths) -> list[dict]:
    p = _path(paths, "vouchers.json")
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_vouchers(paths: Paths, vouchers: list[dict]) -> None:
    atomic_write_json(_path(paths, "vouchers.json"), vouchers)


def get_voucher(paths: Paths, voucher_id: str) -> dict | None:
    for v in load_vouchers(paths):
        if v["voucherId"] == voucher_id:
            return v
    return None


def upsert_voucher(paths: Paths, voucher: dict) -> None:
    vouchers = load_vouchers(paths)
    for i, v in enumerate(vouchers):
        if v["voucherId"] == voucher["voucherId"]:
            vouchers[i] = voucher
            break
    else:
        vouchers.append(voucher)
    save_vouchers(paths, vouchers)


def set_status(paths: Paths, voucher_id: str, status: str, **extra) -> dict | None:
    vouchers = load_vouchers(paths)
    for v in vouchers:
        if v["voucherId"] == voucher_id:
            v["status"] = status
            v["statusAt"] = now_iso()
            v.update(extra)
            save_vouchers(paths, vouchers)
            return v
    return None


def record_tax(paths: Paths, voucher_id: str, tax_result: dict, txn_id: str) -> dict | None:
    vouchers = load_vouchers(paths)
    for v in vouchers:
        if v["voucherId"] == voucher_id:
            v["tax"] = {
                "engineTxnId": txn_id,
                "computedAt": now_iso(),
                "subtotal": tax_result["subtotal"],
                "vendorTaxTotal": tax_result["vendorTaxTotal"],
                "selfAssessedTaxTotal": tax_result["selfAssessedTaxTotal"],
                "recoverableTaxTotal": tax_result["recoverableTaxTotal"],
                "nonrecoverableTaxTotal": tax_result["nonrecoverableTaxTotal"],
                "totalTax": tax_result["totalTax"],
                "jurisdictionId": tax_result["jurisdictionId"],
                "lines": tax_result["lines"],
                "messages": tax_result["messages"],
            }
            save_vouchers(paths, vouchers)
            return v
    return None


def load_outbox(paths: Paths) -> list[dict]:
    p = _path(paths, "outbox.json")
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_outbox(paths: Paths, events: list[dict]) -> None:
    atomic_write_json(_path(paths, "outbox.json"), events)


def mark_event(paths: Paths, event_id: str, status: str) -> None:
    events = load_outbox(paths)
    for ev in events:
        if ev["eventId"] == event_id:
            ev["status"] = status
            ev["statusAt"] = now_iso()
    save_outbox(paths, events)


def load_subledger(paths: Paths) -> list[dict]:
    p = _path(paths, "subledger.json")
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def append_subledger(paths: Paths, voucher: dict, amount: float) -> dict:
    """Idempotent by voucher id."""
    rows = load_subledger(paths)
    for row in rows:
        if row["voucherId"] == voucher["voucherId"]:
            return row
    row = {
        "voucherId": voucher["voucherId"],
        "supplierCode": voucher["supplierCode"],
        "supplierInvoiceNumber": voucher["supplierInvoiceNumber"],
        "amount": round(float(amount), 2),
        "status": "posted",
        "recordedAt": now_iso(),
    }
    rows.append(row)
    atomic_write_json(_path(paths, "subledger.json"), rows)
    return row


def build_estimate_request(cfg: dict, voucher: dict) -> dict:
    """AP-side payload for the tax engine (input/use tax on a purchase)."""
    return {
        "docId": voucher["voucherId"],
        "docType": "PURCHASE",
        "customerCode": voucher["supplierCode"],
        "date": voucher["invoiceDate"],
        "shipTo": {"zip": voucher["deliveryZip"], "countryCode": "US"},
        "shipFrom": {"zip": voucher.get("supplierZip", cfg.get("shipFromZip", "94105")),
                     "countryCode": "US"},
        "lines": [
            {
                "lineId": line["lineId"],
                "quantity": line["quantity"],
                "amount": line["amount"],
                "taxCode": line["taxCode"],
            }
            for line in voucher["lines"]
        ],
        "withholdingApplicable": False,
    }


def compute_voucher_tax(cfg: dict, voucher: dict, engine_result: dict) -> dict:
    """Split engine-determined tax into vendor-charged vs self-assessed and
    recoverable vs non-recoverable, per line."""
    nonrecoverable_codes = set(cfg.get("input_tax", {}).get("nonrecoverable_tax_codes", []))
    engine_lines = {line["lineId"]: line for line in engine_result["lines"]}

    lines: list[dict] = []
    vendor_total = self_total = recov_total = nonrecov_total = 0.0

    for line in voucher["lines"]:
        engine_line = engine_lines.get(line["lineId"], {"jurisdictions": [], "totalTax": 0.0})
        vendor_tax = round(float(line.get("taxCharged", 0.0)), 2)
        self_tax = 0.0 if vendor_tax > 0 else round(float(engine_line["totalTax"]), 2)
        total = round(vendor_tax + self_tax, 2)
        nonrecoverable = line["taxCode"] in nonrecoverable_codes
        recoverable = 0.0 if nonrecoverable else total
        nonrecov = round(total - recoverable, 2)

        vendor_total = round(vendor_total + vendor_tax, 2)
        self_total = round(self_total + self_tax, 2)
        recov_total = round(recov_total + recoverable, 2)
        nonrecov_total = round(nonrecov_total + nonrecov, 2)

        lines.append({
            "lineId": line["lineId"],
            "taxCode": line["taxCode"],
            "amount": round(float(line["amount"]), 2),
            "vendorTax": vendor_tax,
            "selfAssessedTax": self_tax,
            "totalTax": total,
            "recoverable": recoverable,
            "nonrecoverable": nonrecov,
            "jurisdictions": engine_line.get("jurisdictions", []),
        })

    return {
        "subtotal": round(float(voucher["subtotal"]), 2),
        "vendorTaxTotal": vendor_total,
        "selfAssessedTaxTotal": self_total,
        "recoverableTaxTotal": recov_total,
        "nonrecoverableTaxTotal": nonrecov_total,
        "totalTax": round(vendor_total + self_total, 2),
        "jurisdictionId": engine_result.get("jurisdictionId"),
        "stateCode": engine_result.get("stateCode"),
        "nexus": engine_result.get("nexus"),
        "engineTxnId": None,
        "lines": lines,
        "messages": engine_result.get("messages", []),
    }


def match_voucher(cfg: dict, paths: Paths, voucher: dict) -> dict:
    """Three-way match: purchase order vs goods receipt vs vendor invoice.

    Returns MATCHED or the first exception code, with the full variance list.
    Duplicate supplier invoices are detected across the whole population.
    """
    match_cfg = cfg.get("ap", {}).get("three_way_match", {})
    price_tol = float(match_cfg.get("price_tolerance_cents", 0)) / 100.0
    qty_tol = float(match_cfg.get("quantity_tolerance", 0))
    variances: list[dict] = []

    for other in load_vouchers(paths):
        if other["voucherId"] == voucher["voucherId"]:
            continue
        # flag only the later voucher so a duplicate pair blocks exactly once
        if other["voucherId"] > voucher["voucherId"]:
            continue
        if (other["supplierCode"] == voucher["supplierCode"]
                and other["supplierInvoiceNumber"] == voucher["supplierInvoiceNumber"]):
            variances.append({
                "code": "DUPLICATE",
                "detail": f"same supplier invoice number as {other['voucherId']}",
                "duplicateOf": other["voucherId"],
            })
            break

    po_id = voucher.get("poId")
    if not po_id:
        variances.append({"code": "NO_PO", "detail": "no purchase order referenced"})
    else:
        po = _load_po(paths, po_id)
        if po is None:
            variances.append({"code": "PO_NOT_FOUND", "detail": f"{po_id} not in procurement"})
        else:
            po_lines = {line["lineId"]: line for line in po["lines"]}
            for line in voucher["lines"]:
                po_line = po_lines.get(line.get("poLineId") or line["lineId"])
                if po_line is None:
                    variances.append({
                        "code": "PO_LINE_NOT_FOUND",
                        "detail": f"invoice line {line['lineId']} has no PO line",
                    })
                    continue
                if abs(float(line["unitPrice"]) - float(po_line["unitPrice"])) > price_tol:
                    variances.append({
                        "code": "PRICE_VARIANCE",
                        "detail": (f"line {line['lineId']}: invoiced "
                                   f"{float(line['unitPrice']):.2f} vs PO "
                                   f"{float(po_line['unitPrice']):.2f}"),
                    })
                received = _received_qty(paths, po_id, po_line["lineId"])
                if float(line["quantity"]) - received > qty_tol:
                    variances.append({
                        "code": "QUANTITY_VARIANCE",
                        "detail": (f"line {line['lineId']}: invoiced {line['quantity']} "
                                   f"vs received {received}"),
                    })

    status = "MATCHED" if not variances else variances[0]["code"]
    return {
        "status": status,
        "variances": variances,
        "poId": po_id,
        "matchedAt": now_iso(),
    }


def _load_po(paths: Paths, po_id: str) -> dict | None:
    from . import procurement
    return procurement.find_po(paths, po_id)


def _received_qty(paths: Paths, po_id: str, po_line_id: str) -> float:
    from . import procurement
    return procurement.received_quantity(paths, po_id, po_line_id)


def ingest_einvoice_inbox(paths: Paths) -> dict:
    """Parse the e-invoicing inbox (reduced UBL-shaped XML) into the AP store.

    This is the stand-in for an e-invoicing gateway: files land in
    data/einvoicing/inbox and are parsed into vendor-invoice records.
    """
    inbox = paths.einvoicing / "inbox"
    parsed, errors = [], []
    files = sorted(inbox.glob("*.xml")) if inbox.exists() else []
    existing = {v["voucherId"] for v in load_vouchers(paths)}
    for path in files:
        try:
            with open(path, encoding="utf-8") as fh:
                voucher = einvoicing.parse_einvoice(fh.read())
            voucher["einvoiceFile"] = path.name
            if voucher["voucherId"] not in existing:
                upsert_voucher(paths, voucher)
            parsed.append(voucher["voucherId"])
        except Exception as err:  # noqa: BLE001 - one bad file must not stop the batch
            errors.append({"file": path.name, "error": f"{type(err).__name__}: {err}"})
    return {"files": len(files), "parsed": parsed, "errors": errors}
