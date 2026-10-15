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
