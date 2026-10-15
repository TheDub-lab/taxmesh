"""Atlas Tax Engine — Avalara-shaped HTTP service (fictional, demo).

Endpoints mirror the shape of a real indirect-tax engine:
  POST /v2/estimate     calculate tax by jurisdiction (calculate)
  POST /v2/commit       persist the document to the transaction log (commit)
  POST /v2/void/{id}    void a committed transaction
  GET  /v2/transactions transaction log (recon extract)
  GET  /v2/nexus        registered jurisdictions
  POST /v2/certs/validate  exemption-certificate coverage check
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..config import get_paths, load_config
from ..util import now_iso
from .rules import TaxRuleError, estimate as rules_estimate, find_cert
from .store import JsonStore


class AddressModel(BaseModel):
    zip: str
    countryCode: str = "US"


class LineModel(BaseModel):
    lineId: str
    quantity: float = 1.0
    amount: float
    taxCode: str


class EstimateRequest(BaseModel):
    docId: str
    docType: str = "SALE"
    customerCode: str
    date: str
    shipTo: AddressModel
    shipFrom: AddressModel | None = None
    lines: list[LineModel] = Field(min_length=1)
    withholdingApplicable: bool = False


class CertValidateRequest(BaseModel):
    customerCode: str
    stateCode: str
    date: str


def _rule_http_error(err: TaxRuleError) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": err.code, "message": err.message})


def create_app(cfg: dict | None = None, root=None) -> FastAPI:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()

    txn_store = JsonStore(paths.engine / "transactions.json", {"next_seq": 1, "items": []})
    cert_store = JsonStore(paths.engine / "certs.json", [])

    app = FastAPI(title="Atlas Tax Engine (taxmesh demo)", version="0.1.0")

    @app.get("/v2/health")
    def health():
        return {"status": "ok", "service": "atlas-tax-engine", "nexus": cfg["nexus"]}

    @app.get("/v2/nexus")
    def nexus():
        return {"registered": cfg["nexus"]}

    @app.post("/v2/estimate")
    def estimate(req: EstimateRequest):
        try:
            return rules_estimate(cfg, req.model_dump(), cert_store.load())
        except TaxRuleError as err:
            raise _rule_http_error(err)

    @app.post("/v2/commit")
    def commit(req: EstimateRequest):
        try:
            result = rules_estimate(cfg, req.model_dump(), cert_store.load())
        except TaxRuleError as err:
            raise _rule_http_error(err)

        state = txn_store.load()
        for txn in state["items"]:
            if txn["docId"] == req.docId and not txn.get("voided"):
                return {**txn, "duplicate": True}

        seq = state["next_seq"]
        txn = {
            "txnId": f"TXN-{seq:06d}",
            "docId": req.docId,
            "docType": req.docType,
            "committedAt": now_iso(),
            "requestHash": result["requestHash"],
            "jurisdictionId": result["jurisdictionId"],
            "stateCode": result["stateCode"],
            "nexus": result["nexus"],
            "exemptCertId": result["exemptCertId"],
            "subtotal": result["subtotal"],
            "totalTax": result["totalTax"],
            "totalExempt": result["totalExempt"],
            "withholding": result["withholding"],
            "lines": result["lines"],
            "messages": result["messages"],
            "voided": False,
        }
        state["items"].append(txn)
        state["next_seq"] = seq + 1
        txn_store.save(state)
        return {**txn, "duplicate": False}

    @app.post("/v2/void/{txn_id}")
    def void_txn(txn_id: str):
        state = txn_store.load()
        for txn in state["items"]:
            if txn["txnId"] == txn_id:
                txn["voided"] = True
                txn["voidedAt"] = now_iso()
                txn_store.save(state)
                return txn
        raise HTTPException(status_code=404, detail={"code": "TXN_NOT_FOUND", "message": txn_id})

    @app.get("/v2/transactions")
    def transactions(include_voided: bool = False):
        state = txn_store.load()
        items = [t for t in state["items"] if include_voided or not t.get("voided")]
        return {"count": len(items), "transactions": items}

    @app.get("/v2/certs")
    def list_certs():
        return {"count": len(cert_store.load()), "certificates": cert_store.load()}

    @app.post("/v2/certs/validate")
    def validate_cert(req: CertValidateRequest):
        cert = find_cert(cert_store.load(), req.customerCode, req.stateCode, req.date)
        if cert:
            return {"valid": True, "certId": cert["certId"], "certType": cert.get("certType"),
                    "coversStates": cert["coversStates"], "reason": "covered"}
        return {"valid": False, "certId": None, "reason": "no certificate covers this customer/state/date"}

    @app.post("/v2/admin/reload-certs")
    def reload_certs():
        """Admin seam: re-read certificates from the CRM extract file."""
        import json as _json
        if not (paths.crm / "certificates.json").exists():
            raise HTTPException(404, detail={"code": "CRM_EXTRACT_MISSING",
                                             "message": "data/crm/certificates.json not found"})
        with open(paths.crm / "certificates.json", encoding="utf-8") as fh:
            certs = _json.load(fh)
        cert_store.save(certs)
        return {"loaded": len(certs)}

    return app
