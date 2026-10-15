"""Atlas engine HTTP surface: estimate/commit idempotency, errors, cert validation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.engine.app import create_app
from src.integrations.engine_client import EngineClient, EngineError


def _payload(doc_id="INV-API-1", zip_code="94105", code="SAAS-SUB", amount=500.0,
             customer="CUST-001"):
    return {
        "docId": doc_id,
        "docType": "SALE",
        "customerCode": customer,
        "date": "2026-09-15",
        "shipTo": {"zip": zip_code, "countryCode": "US"},
        "lines": [{"lineId": "1", "quantity": 1, "amount": amount, "taxCode": code}],
    }


def test_health_and_nexus(engine):
    health = engine.health()
    assert health["status"] == "ok"
    assert "US-CA" in health["nexus"]


def test_estimate_and_commit_and_duplicate_commit(engine):
    est = engine.estimate(_payload())
    assert est["totalTax"] == 40.63  # 500 * 8.125%

    txn = engine.commit(_payload())
    assert txn["txnId"] == "TXN-000001"
    assert txn["duplicate"] is False
    assert txn["totalTax"] == 40.63

    again = engine.commit(_payload())
    assert again["duplicate"] is True
    assert again["txnId"] == "TXN-000001"

    txns = engine.transactions()
    assert len(txns) == 1


def test_unmapped_zip_returns_structured_422(engine):
    try:
        engine.estimate(_payload(zip_code="00099"))
        raise AssertionError("expected EngineError")
    except EngineError as err:
        assert err.status_code == 422
        assert err.code == "ZIP_NOT_MAPPED"


def test_commit_persists_line_jurisdiction_detail(engine):
    txn = engine.commit(_payload(amount=1000.0))
    line = txn["lines"][0]
    assert line["totalTax"] == 81.25
    assert {r["jurisdictionType"] for r in line["jurisdictions"]} == {
        "State", "County", "City", "District",
    }
    assert txn["stateCode"] == "US-CA"


def test_cert_validate_endpoint(workspace, engine):
    from src.sources.seed import run_seed

    run_seed(cfg=workspace.cfg, root=workspace.root)
    app = create_app(cfg=workspace.cfg, root=workspace.root)
    client = TestClient(app)
    loaded = client.post("/v2/admin/reload-certs").json()
    assert loaded["loaded"] == 3
    ok = engine.validate_cert(
        {"customerCode": "CUST-002", "stateCode": "US-CA", "date": "2026-09-15"}
    )
    assert ok["valid"] is True and ok["certId"] == "CERT-CA-1001"
    bad = engine.validate_cert(
        {"customerCode": "CUST-002", "stateCode": "US-NY", "date": "2026-09-15"}
    )
    assert bad["valid"] is False
