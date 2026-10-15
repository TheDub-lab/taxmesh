"""Fincore ERP (NetSuite/SAP-flavored) synthetic source: customers + GL journal."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Paths
from ..util import atomic_write_json, now_iso


def _journal_path(paths: Paths) -> Path:
    return paths.erp / "journal.json"


def load_customers(paths: Paths) -> list[dict]:
    p = paths.erp / "customers.json"
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def find_customer(paths: Paths, customer_code: str) -> dict | None:
    for cust in load_customers(paths):
        if cust["customerCode"] == customer_code:
            return cust
    return None


def load_journal(paths: Paths) -> list[dict]:
    if not _journal_path(paths).exists():
        return []
    with open(_journal_path(paths), encoding="utf-8") as fh:
        return json.load(fh)


def save_journal(paths: Paths, entries: list[dict]) -> None:
    atomic_write_json(_journal_path(paths), entries)


def post_invoice_entry(cfg: dict, paths: Paths, invoice: dict, tax_result: dict) -> dict:
    """Post the double-entry for a taxed invoice. Idempotent by invoice id.

    DR 1200 AR            subtotal + tax + withholding
    CR 4000 Revenue       subtotal
    CR 2100 Tax Payable   per jurisdiction (from engine lines)
    CR 2105 Withholding   when engine applied withholding
    """
    entry_id = f"JE-{invoice['invoiceId']}"
    journal = load_journal(paths)
    for entry in journal:
        if entry["entryId"] == entry_id:
            return entry

    accounts = cfg["gl_accounts"]
    subtotal = float(invoice["subtotal"])
    tax_total = float(tax_result["totalTax"])
    wh_total = float(tax_result["withholding"]["amount"])

    tax_by_juris: dict[str, float] = {}
    for line in tax_result["lines"]:
        for row in line["jurisdictions"]:
            jid = row["jurisdictionId"]
            tax_by_juris[jid] = round(tax_by_juris.get(jid, 0.0) + row["tax"], 2)

    rows = [
        {"account": accounts["AR"], "debit": round(subtotal + tax_total + wh_total, 2),
         "credit": 0.0, "jurisdictionId": None},
        {"account": accounts["REVENUE"], "debit": 0.0, "credit": round(subtotal, 2),
         "jurisdictionId": None},
    ]
    for jid, amount in sorted(tax_by_juris.items()):
        if amount:
            rows.append({"account": accounts["SALES_TAX_PAYABLE"], "debit": 0.0,
                         "credit": amount, "jurisdictionId": jid})
    if wh_total:
        rows.append({"account": accounts["WITHHOLDING_PAYABLE"], "debit": 0.0,
                     "credit": wh_total, "jurisdictionId": None})

    debit = round(sum(r["debit"] for r in rows), 2)
    credit = round(sum(r["credit"] for r in rows), 2)
    if debit != credit:
        raise ValueError(f"unbalanced entry {entry_id}: DR {debit} != CR {credit}")

    entry = {
        "entryId": entry_id,
        "invoiceId": invoice["invoiceId"],
        "date": invoice["issueDate"],
        "postedAt": now_iso(),
        "lines": rows,
    }
    journal.append(entry)
    save_journal(paths, journal)
    return entry


def post_vendor_entry(cfg: dict, paths: Paths, voucher: dict, tax_result: dict) -> dict:
    """Post the AP double-entry for a matched, taxed vendor invoice.

    DR 5000 Expense                subtotal
    DR 1300 Input tax recoverable  recoverable tax, per jurisdiction
    DR 6100 Non-recoverable tax    non-recoverable tax
    CR 2110 AP                     subtotal + vendor-charged tax
    CR 2200 Use tax payable        self-assessed use tax, per jurisdiction
    """
    entry_id = f"JEAP-{voucher['voucherId']}"
    journal = load_journal(paths)
    for entry in journal:
        if entry["entryId"] == entry_id:
            return entry

    accounts = cfg["gl_accounts"]
    subtotal = round(float(voucher["subtotal"]), 2)
    vendor_total = round(float(tax_result["vendorTaxTotal"]), 2)
    self_total = round(float(tax_result["selfAssessedTaxTotal"]), 2)
    recov_total = round(float(tax_result["recoverableTaxTotal"]), 2)
    nonrecov_total = round(float(tax_result["nonrecoverableTaxTotal"]), 2)

    recov_by_juris: dict[str, float] = {}
    self_by_juris: dict[str, float] = {}
    voucher_juris = tax_result.get("jurisdictionId")
    for line in tax_result["lines"]:
        rows = line["jurisdictions"]
        # self-assessed portion is jurisdiction-split by the engine's own rows
        if line["selfAssessedTax"] > 0:
            for row in rows:
                jid = row["jurisdictionId"]
                self_by_juris[jid] = round(self_by_juris.get(jid, 0.0) + row["tax"], 2)
                if line["recoverable"] > 0:
                    recov_by_juris[jid] = round(recov_by_juris.get(jid, 0.0) + row["tax"], 2)
        # vendor-charged tax has no engine split: allocate to the voucher jurisdiction
        if line["vendorTax"] > 0 and line["recoverable"] > 0 and voucher_juris:
            recov_by_juris[voucher_juris] = round(
                recov_by_juris.get(voucher_juris, 0.0) + line["vendorTax"], 2)

    rows = [
        {"account": accounts["EXPENSE"], "debit": subtotal, "credit": 0.0,
         "jurisdictionId": None},
    ]
    for jid, amount in sorted(recov_by_juris.items()):
        if amount:
            rows.append({"account": accounts["INPUT_TAX_RECOVERABLE"], "debit": amount,
                         "credit": 0.0, "jurisdictionId": jid})
    if nonrecov_total:
        rows.append({"account": accounts["NONRECOVERABLE_TAX"], "debit": nonrecov_total,
                     "credit": 0.0, "jurisdictionId": None})
    rows.append({"account": accounts["AP"], "debit": 0.0,
                 "credit": round(subtotal + vendor_total, 2), "jurisdictionId": None})
    for jid, amount in sorted(self_by_juris.items()):
        if amount:
            rows.append({"account": accounts["USE_TAX_PAYABLE"], "debit": 0.0,
                         "credit": amount, "jurisdictionId": jid})

    debit = round(sum(r["debit"] for r in rows), 2)
    credit = round(sum(r["credit"] for r in rows), 2)
    if debit != credit:
        raise ValueError(f"unbalanced AP entry {entry_id}: DR {debit} != CR {credit}")

    entry = {
        "entryId": entry_id,
        "voucherId": voucher["voucherId"],
        "supplierCode": voucher["supplierCode"],
        "date": voucher["invoiceDate"],
        "postedAt": now_iso(),
        "lines": rows,
    }
    journal.append(entry)
    save_journal(paths, journal)
    return entry
