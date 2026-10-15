"""Parallel run: reconcile the new AP pipeline against the legacy process.

For every vendor invoice we compare the amount the new system posted to AP
against the amount the legacy process posted, then *classify* each difference.
Only classified (expected) differences are tolerated; anything unclassified is
an unexpected variance and blocks go-live.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Paths, get_paths, load_config
from ..sources import ap, erp, legacy_ap
from ..util import atomic_write_json, new_run_id, now_iso

EXPECTED_CLASSES = (
    "OK",
    "EXPECTED_MISSING_USE_TAX",
    "EXPECTED_ROUNDING",
    "EXPECTED_BLOCKED_UNMATCHED",
)


def _new_ap_amounts(paths: Paths) -> dict[str, dict]:
    """New-system *recognised cost* per voucher (total debits), plus self tax.

    Self-assessed use tax is credited to Use Tax Payable, not AP, so comparing
    AP credit alone would hide it. Total debits = subtotal + recoverable +
    non-recoverable tax = subtotal + total tax, which differs from the legacy
    amount (subtotal + vendor-charged tax) by exactly the self-assessed tax.
    """
    debit_by_voucher: dict[str, float] = {}
    for entry in erp.load_journal(paths):
        vid = entry.get("voucherId")
        if not vid:
            continue
        debit_by_voucher[vid] = round(
            debit_by_voucher.get(vid, 0.0)
            + sum(l["debit"] for l in entry["lines"]),
            2,
        )
    out: dict[str, dict] = {}
    for voucher in ap.load_vouchers(paths):
        vid = voucher["voucherId"]
        tax = voucher.get("tax") or {}
        out[vid] = {
            "amount": debit_by_voucher.get(vid, 0.0),
            "posted": voucher["status"] == "posted",
            "status": voucher["status"],
            "selfAssessedTax": round(float(tax.get("selfAssessedTaxTotal", 0.0)), 2),
        }
    return out


def run_parallel_run(
    cfg: dict | None = None,
    root=None,
    inject: list[str] | None = None,
    write: bool = True,
) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()

    legacy = legacy_ap.run_legacy_ap(cfg, paths, inject=inject)
    new_side = _new_ap_amounts(paths)

    items: list[dict] = []
    class_counts: dict[str, int] = {}
    for posting in legacy["postings"]:
        vid = posting["voucherId"]
        new = new_side.get(vid, {"amount": 0.0, "posted": False, "status": "unknown",
                                 "selfAssessedTax": 0.0})
        legacy_amount = round(float(posting["amount"]), 2)
        new_amount = round(float(new["amount"]), 2)
        delta = round(new_amount - legacy_amount, 2)

        if delta == 0:
            classification = "OK"
        elif not new["posted"]:
            classification = "EXPECTED_BLOCKED_UNMATCHED"
        elif delta == new["selfAssessedTax"]:
            classification = "EXPECTED_MISSING_USE_TAX"
        elif abs(delta) <= 0.01:
            classification = "EXPECTED_ROUNDING"
        else:
            classification = "UNEXPECTED"

        class_counts[classification] = class_counts.get(classification, 0) + 1
        items.append({
            "voucherId": vid,
            "supplierCode": posting["supplierCode"],
            "legacyAmount": legacy_amount,
            "newAmount": new_amount,
            "delta": delta,
            "classification": classification,
            "newStatus": new["status"],
        })

    unexpected = [i for i in items if i["classification"] not in EXPECTED_CLASSES]
    expected = [i for i in items if i["classification"] in EXPECTED_CLASSES
                and i["classification"] != "OK"]

    result = {
        "runId": new_run_id("parallel_run"),
        "generatedAt": now_iso(),
        "status": "PASS" if not unexpected else "FAIL",
        "processes": {"legacy": legacy["process"], "new": "taxmesh-ap-v1"},
        "counts": {
            "compared": len(items),
            "ok": class_counts.get("OK", 0),
            "expected": len(expected),
            "unexpected": len(unexpected),
        },
        "classificationCounts": class_counts,
        "totals": {
            "legacyAmount": round(sum(i["legacyAmount"] for i in items), 2),
            "newAmount": round(sum(i["newAmount"] for i in items), 2),
            "variance": round(sum(i["delta"] for i in items), 2),
        },
        "expectedVariances": expected,
        "unexpectedVariances": unexpected,
        "knownLegacyGaps": legacy["knownGaps"],
        "injected": inject or [],
    }

    if write:
        paths.parallel_run.mkdir(parents=True, exist_ok=True)
        out: Path = paths.parallel_run / f"{result['runId']}.json"
        atomic_write_json(out, result)
        result["outputPath"] = str(out)
    return result
