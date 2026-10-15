"""Hand-computed fixtures for the Atlas tax rules."""

from __future__ import annotations

import pytest

from src.config import load_config
from src.engine.rules import TaxRuleError, estimate, find_cert, resolve_jurisdiction
from src.sources.seed import CERTS

CFG = load_config()


def req(**overrides):
    base = {
        "docId": "INV-T1",
        "docType": "SALE",
        "customerCode": "CUST-001",
        "date": "2026-09-15",
        "shipTo": {"zip": "94105", "countryCode": "US"},
        "lines": [{"lineId": "1", "quantity": 1, "amount": 1000.0, "taxCode": "SAAS-SUB"}],
        "withholdingApplicable": False,
    }
    base.update(overrides)
    return base


def test_ca_saas_line_matches_hand_computed_rates():
    result = estimate(CFG, req(), certs=[])
    assert result["jurisdictionId"] == "US-CA-SF"
    assert result["nexus"] is True
    assert result["totalTax"] == 81.25  # 1000 * 8.125%
    by_type = {r["jurisdictionType"]: r["tax"] for r in result["lines"][0]["jurisdictions"]}
    assert by_type == {"State": 60.0, "County": 12.5, "City": 5.0, "District": 3.75}
    assert result["lines"][0]["taxable"] is True


def test_professional_services_exempt_in_california():
    result = estimate(
        CFG, req(lines=[{"lineId": "1", "quantity": 1, "amount": 1000.0,
                         "taxCode": "PROF-SVC"}]), certs=[]
    )
    assert result["totalTax"] == 0.0
    assert result["totalExempt"] == 1000.0
    assert result["lines"][0]["taxable"] is False


def test_shipping_taxable_in_new_york_not_california():
    ny = estimate(
        CFG,
        req(shipTo={"zip": "10001", "countryCode": "US"},
            lines=[{"lineId": "1", "quantity": 1, "amount": 100.0, "taxCode": "SHIP"}]),
        certs=[],
    )
    assert ny["totalTax"] == 8.88  # 4.00 + 4.50 + 0.38 (half-up cents per jurisdiction)
    ca = estimate(
        CFG,
        req(lines=[{"lineId": "1", "quantity": 1, "amount": 100.0, "taxCode": "SHIP"}]),
        certs=[],
    )
    assert ca["totalTax"] == 0.0
    assert ca["totalExempt"] == 100.0


def test_non_nexus_state_returns_zero_tax_with_message():
    result = estimate(
        CFG, req(shipTo={"zip": "80202", "countryCode": "US"}), certs=[]
    )
    assert result["nexus"] is False
    assert result["totalTax"] == 0.0
    assert any(m["code"] == "NO_NEXUS" for m in result["messages"])


def test_valid_certificate_exempts_document():
    result = estimate(CFG, req(customerCode="CUST-002"), certs=CERTS)
    assert result["totalTax"] == 0.0
    assert result["exemptCertId"] == "CERT-CA-1001"
    assert result["totalExempt"] == 1000.0


def test_expired_certificate_does_not_exempt():
    certs = [c for c in CERTS if c["certId"] == "CERT-WA-1003"]
    result = estimate(
        CFG, req(customerCode="CUST-008", shipTo={"zip": "98101", "countryCode": "US"}),
        certs=certs,
    )
    assert result["exemptCertId"] is None
    assert result["totalTax"] > 0


def test_certificate_state_and_date_coverage():
    assert find_cert(CERTS, "CUST-002", "US-CA", "2026-09-15") is not None
    assert find_cert(CERTS, "CUST-002", "US-NY", "2026-09-15") is None  # state not covered
    assert find_cert(CERTS, "CUST-008", "US-WA", "2026-09-15") is None  # expired
    assert find_cert(CERTS, "CUST-008", "US-WA", "2026-05-01") is not None


def test_withholding_applies_to_flagged_customers():
    result = estimate(CFG, req(withholdingApplicable=True), certs=[])
    assert result["withholding"] == {"applies": True, "rate": 0.019, "amount": 19.0}
    plain = estimate(CFG, req(withholdingApplicable=False), certs=[])
    assert plain["withholding"]["applies"] is False
    assert plain["withholding"]["amount"] == 0.0


def test_unknown_zip_raises_rule_error():
    with pytest.raises(TaxRuleError) as err:
        estimate(CFG, req(shipTo={"zip": "00099", "countryCode": "US"}), certs=[])
    assert err.value.code == "ZIP_NOT_MAPPED"


def test_unknown_tax_code_raises_rule_error():
    with pytest.raises(TaxRuleError) as err:
        estimate(CFG, req(lines=[{"lineId": "1", "quantity": 1, "amount": 10.0,
                                  "taxCode": "NOPE"}]), certs=[])
    assert err.value.code == "TAX_CODE_NOT_FOUND"


def test_bad_date_and_empty_lines_rejected():
    with pytest.raises(TaxRuleError) as err:
        estimate(CFG, req(date="09/15/2026"), certs=[])
    assert err.value.code == "DATE_INVALID"
    with pytest.raises(TaxRuleError) as err:
        estimate(CFG, req(lines=[]), certs=[])
    assert err.value.code == "NO_LINES"


def test_resolve_jurisdiction_and_nexus():
    jid, juris, state = resolve_jurisdiction(CFG, "94105")
    assert (jid, state) == ("US-CA-SF", "US-CA")
    assert juris["rates"]["state"] == 0.06
    assert "US-CA" in CFG["nexus"]
