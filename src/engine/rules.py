"""Pure tax-determination rules for the Atlas engine (demo domain logic).

Everything here is framework-free so it can be unit-tested against
hand-computed fixtures. All rates come from config.json and are
ILLUSTRATIVE synthetic values.
"""

from __future__ import annotations

from datetime import date as _date

from ..util import cents, d2f, dec, short_hash

COMPONENT_LABELS = {
    "state": "State",
    "county": "County",
    "city": "City",
    "district": "District",
}


class TaxRuleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def resolve_jurisdiction(cfg: dict, zip_code: str):
    jid = cfg["zip_to_jurisdiction"].get(str(zip_code))
    if jid is None:
        raise TaxRuleError("ZIP_NOT_MAPPED", f"zip {zip_code} is not mapped to a jurisdiction")
    juris = cfg["jurisdictions"].get(jid)
    if juris is None:
        raise TaxRuleError("JURISDICTION_NOT_CONFIGURED", f"{jid} has no rate configuration")
    return jid, juris, juris["state_code"]


def is_nexus(cfg: dict, state_code: str) -> bool:
    return state_code in cfg.get("nexus", [])


def find_cert(certs: list[dict], customer_code: str, state_code: str, invoice_date: str) -> dict | None:
    for cert in certs or []:
        if cert.get("customerCode") != customer_code:
            continue
        if state_code not in cert.get("coversStates", []):
            continue
        if cert.get("validFrom", "9999-12-31") <= invoice_date <= cert.get("validTo", "0000-01-01"):
            return cert
    return None


def estimate(cfg: dict, req: dict, certs: list[dict]) -> dict:
    try:
        _date.fromisoformat(req["date"])
    except (KeyError, ValueError):
        raise TaxRuleError("DATE_INVALID", "date must be ISO YYYY-MM-DD")

    lines_in = req.get("lines") or []
    if not lines_in:
        raise TaxRuleError("NO_LINES", "estimate requires at least one line")

    juris_id, juris, state_code = resolve_jurisdiction(cfg, req["shipTo"]["zip"])
    nexus = is_nexus(cfg, state_code)
    messages = []
    if not nexus:
        messages.append({
            "code": "NO_NEXUS",
            "text": f"no nexus registered for {state_code}; no tax calculated",
        })

    cert = find_cert(certs, req.get("customerCode", ""), state_code, req["date"])
    exempt_via_cert = cert is not None
    if exempt_via_cert:
        messages.append({
            "code": "EXEMPT_CERT",
            "text": f"certificate {cert['certId']} exempts {state_code} for this document",
        })

    lines_out = []
    total_tax = 0.0
    total_exempt = 0.0
    subtotal = 0.0

    for line in lines_in:
        code = line["taxCode"]
        rule = cfg["tax_codes"].get(code)
        if rule is None:
            raise TaxRuleError("TAX_CODE_NOT_FOUND", f"tax code '{code}' is not configured")

        amount = cents(line["amount"])
        if amount < 0:
            raise TaxRuleError("NEGATIVE_LINE", f"line {line['lineId']} has a negative amount")
        subtotal += float(amount)

        taxable = bool(rule["taxable_states"].get(state_code, False)) and nexus and not exempt_via_cert
        rows = []
        line_tax = 0.0
        if taxable:
            for component, rate in juris["rates"].items():
                rate_d = dec(rate)
                if rate_d <= 0:
                    continue
                tax = cents(amount * rate_d)
                rows.append({
                    "jurisdictionId": juris_id,
                    "jurisdictionType": COMPONENT_LABELS[component],
                    "rate": float(rate_d),
                    "taxableAmount": d2f(amount),
                    "tax": d2f(tax),
                })
                line_tax += float(tax)

        total_tax += line_tax
        if not taxable:
            total_exempt += float(amount)

        lines_out.append({
            "lineId": line["lineId"],
            "taxCode": code,
            "amount": d2f(amount),
            "taxable": taxable,
            "exemptAmount": 0.0 if taxable else d2f(amount),
            "totalTax": line_tax,
            "jurisdictions": rows,
        })

    withholding = {"applies": False, "rate": 0.0, "amount": 0.0}
    if req.get("withholdingApplicable") and cfg.get("withholding", {}).get(
        "applies_to_flagged_customers_only", True
    ):
        wh_rate = dec(cfg["withholding"]["rate"])
        wh_amount = cents(dec(subtotal) * wh_rate)
        withholding = {"applies": True, "rate": float(wh_rate), "amount": d2f(wh_amount)}
        messages.append({
            "code": "WITHHOLDING_APPLIED",
            "text": f"withholding at {float(wh_rate):.3%} applied to flagged customer document",
        })

    return {
        "docId": req["docId"],
        "docType": req.get("docType", "SALE"),
        "customerCode": req.get("customerCode"),
        "date": req["date"],
        "jurisdictionId": juris_id,
        "stateCode": state_code,
        "nexus": nexus,
        "exemptCertId": cert["certId"] if cert else None,
        "currency": cfg.get("currency", "USD"),
        "subtotal": d2f(subtotal),
        "totalTax": round(total_tax, 2),
        "totalExempt": round(total_exempt, 2),
        "withholding": withholding,
        "lines": lines_out,
        "messages": messages,
        "requestHash": short_hash(req),
    }
