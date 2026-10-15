"""Legacy AP process — the current-state baseline being migrated away from.

This is a deliberately simplified, "before" process used for the parallel run:
it posts every vendor invoice (no three-way match gate), recognises only
vendor-charged tax (no self-assessed use tax), and rounds with banker's
rounding instead of half-up cents. Those gaps are the *expected* variances the
parallel run is designed to surface; anything else is an unexpected variance and
blocks go-live.
"""

from __future__ import annotations

from ..config import Paths
from . import ap


def run_legacy_ap(cfg: dict, paths: Paths, inject: list[str] | None = None) -> dict:
    inject = list(inject or [])
    vouchers = ap.load_vouchers(paths)
    postings = []
    drift_voucher = None

    for voucher in vouchers:
        vendor_tax = round(float(voucher.get("taxChargedTotal", 0.0)), 2)
        subtotal = round(float(voucher["subtotal"]), 2)
        amount = round(subtotal + vendor_tax, 2)
        postings.append({
            "voucherId": voucher["voucherId"],
            "supplierCode": voucher["supplierCode"],
            "subtotal": subtotal,
            "vendorTax": vendor_tax,
            "amount": amount,
        })

    if "legacy_drift" in inject and postings:
        by_id = {p["voucherId"]: p for p in postings}
        # drift a voucher the legacy process already agrees on (no self-assessed
        # use tax), so the injected error surfaces as an unclassified variance
        target = next(
            (by_id[v["voucherId"]] for v in vouchers
             if v["voucherId"] in by_id
             and float((v.get("tax") or {}).get("selfAssessedTaxTotal", 0.0)) == 0.0),
            postings[min(3, len(postings) - 1)],
        )
        target["amount"] = round(target["amount"] + 100.00, 2)
        target["drift"] = 100.00
        drift_voucher = target["voucherId"]

    totals = {
        "postings": len(postings),
        "subtotal": round(sum(p["subtotal"] for p in postings), 2),
        "vendorTax": round(sum(p["vendorTax"] for p in postings), 2),
        "amount": round(sum(p["amount"] for p in postings), 2),
    }
    return {
        "process": "legacy-ap-v1",
        "postings": postings,
        "totals": totals,
        "knownGaps": [
            "no three-way match gate: unmatched invoices are posted",
            "vendor-charged tax only: self-assessed use tax is not accrued",
            "banker's rounding instead of half-up cents",
        ],
        "injectedDrift": drift_voucher,
    }
