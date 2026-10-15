"""Payline Billing (Stripe-flavored) synthetic source: invoices + event outbox."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Paths
from ..util import atomic_write_json, now_iso


def _invoices_path(paths: Paths) -> Path:
    return paths.billing / "invoices.json"


def _outbox_path(paths: Paths) -> Path:
    return paths.billing / "outbox.json"


def load_invoices(paths: Paths) -> list[dict]:
    if not _invoices_path(paths).exists():
        return []
    with open(_invoices_path(paths), encoding="utf-8") as fh:
        return json.load(fh)


def save_invoices(paths: Paths, invoices: list[dict]) -> None:
    atomic_write_json(_invoices_path(paths), invoices)


def get_invoice(paths: Paths, invoice_id: str) -> dict | None:
    for inv in load_invoices(paths):
        if inv["invoiceId"] == invoice_id:
            return inv
    return None


def load_outbox(paths: Paths) -> list[dict]:
    if not _outbox_path(paths).exists():
        return []
    with open(_outbox_path(paths), encoding="utf-8") as fh:
        return json.load(fh)


def save_outbox(paths: Paths, events: list[dict]) -> None:
    atomic_write_json(_outbox_path(paths), events)


def mark_event(paths: Paths, event_id: str, status: str) -> None:
    events = load_outbox(paths)
    for ev in events:
        if ev["eventId"] == event_id:
            ev["status"] = status
            ev["statusAt"] = now_iso()
    save_outbox(paths, events)


def build_estimate_request(cfg: dict, invoice: dict, customer: dict | None) -> dict:
    """Billing-side payload for the tax engine (API contract documented in docs/interfaces.md)."""
    return {
        "docId": invoice["invoiceId"],
        "docType": "SALE",
        "customerCode": invoice["customerCode"],
        "date": invoice["issueDate"],
        "shipTo": {"zip": invoice["shipToZip"], "countryCode": "US"},
        "shipFrom": {"zip": cfg.get("shipFromZip", "94105"), "countryCode": "US"},
        "lines": [
            {
                "lineId": line["lineId"],
                "quantity": line["quantity"],
                "amount": line["amount"],
                "taxCode": line["taxCode"],
            }
            for line in invoice["lines"]
        ],
        "withholdingApplicable": bool((customer or {}).get("withholdingApplicable", False)),
    }


def record_tax(paths: Paths, invoice_id: str, tax_result: dict, txn_id: str) -> dict | None:
    invoices = load_invoices(paths)
    for inv in invoices:
        if inv["invoiceId"] == invoice_id:
            inv["tax"] = {
                "engineTxnId": txn_id,
                "computedAt": now_iso(),
                "subtotal": tax_result["subtotal"],
                "totalTax": tax_result["totalTax"],
                "totalExempt": tax_result["totalExempt"],
                "withholding": tax_result["withholding"],
                "jurisdictionId": tax_result["jurisdictionId"],
                "lines": tax_result["lines"],
                "messages": tax_result["messages"],
            }
            save_invoices(paths, invoices)
            return inv
    return None
