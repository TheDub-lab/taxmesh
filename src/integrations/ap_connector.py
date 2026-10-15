"""Vendor invoice -> three-way match -> input/use tax -> GL -> subledger -> mart.

Idempotent at every step so at-least-once delivery is safe:
  1. three-way match (PO vs goods receipt vs invoice) — exceptions are blocked
  2. estimate + commit input/use tax at the engine (idempotent by docId)
  3. post the AP double entry in the ERP GL (idempotent by entry id)
  4. load the voucher facts into the tax mart, and record the AP subledger row

Note the distinction the bus relies on: a *match exception* is a business
outcome (the voucher is blocked and the event is done), while an engine or
transport error is a delivery failure (retry, then dead-letter).
"""

from __future__ import annotations

from ..config import Paths
from ..sources import ap, erp, tax_mart
from .bus import IdempotencyStore, RunManifest, deliver
from .engine_client import EngineClient


class ConnectorError(Exception):
    pass


class ApConnector:
    def __init__(
        self,
        cfg: dict,
        paths: Paths,
        engine: EngineClient,
        manifest: RunManifest | None = None,
        idem: IdempotencyStore | None = None,
        max_attempts: int = 3,
        backoff_s: float = 0.0,
    ):
        self.cfg = cfg
        self.paths = paths
        self.engine = engine
        self.manifest = manifest
        self.idem = idem
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s

    def process_voucher(self, voucher: dict) -> dict:
        voucher_id = voucher["voucherId"]
        match = ap.match_voucher(self.cfg, self.paths, voucher)
        ap.upsert_voucher(self.paths, {**voucher, "match": match})

        if match["status"] != "MATCHED":
            ap.set_status(self.paths, voucher_id, "blocked", match=match)
            return {
                "voucherId": voucher_id,
                "outcome": "BLOCKED",
                "matchStatus": match["status"],
                "variances": match["variances"],
            }

        if voucher.get("tax") is None:
            payload = ap.build_estimate_request(self.cfg, voucher)
            estimate = self.engine.estimate(payload)
            txn = self.engine.commit(payload)
            tax_result = ap.compute_voucher_tax(self.cfg, voucher, estimate)
            tax_result["engineTxnId"] = txn["txnId"]
            voucher = ap.record_tax(self.paths, voucher_id, tax_result, txn["txnId"])
            if voucher is None:
                raise ConnectorError(f"voucher {voucher_id} vanished during processing")

        tax_result = voucher["tax"]
        entry = erp.post_vendor_entry(self.cfg, self.paths, voucher, tax_result)
        ap.append_subledger(self.paths, voucher,
                            round(float(voucher["subtotal"]) + float(tax_result["vendorTaxTotal"]), 2))
        tax_mart.load_vendor_invoice(self.paths, voucher, tax_result, entry, match["status"])
        ap.set_status(self.paths, voucher_id, "posted",
                      glEntryId=entry["entryId"], match=match)

        return {
            "voucherId": voucher_id,
            "outcome": "POSTED",
            "matchStatus": match["status"],
            "txnId": tax_result["engineTxnId"],
            "entryId": entry["entryId"],
            "subtotal": float(voucher["subtotal"]),
            "vendorTax": float(tax_result["vendorTaxTotal"]),
            "selfAssessedTax": float(tax_result["selfAssessedTaxTotal"]),
            "totalTax": float(tax_result["totalTax"]),
        }

    def run(self) -> dict:
        events = sorted(
            (e for e in ap.load_outbox(self.paths) if e["status"] == "pending"),
            key=lambda e: e["eventId"],
        )
        posted, blocked, skipped, dlq = [], [], [], []

        for event in events:
            idem_key = f"voucher:{event['voucherId']}"
            if self.idem is not None and self.idem.get(idem_key) is not None:
                ap.mark_event(self.paths, event["eventId"], "processed")
                skipped.append(event["eventId"])
                if self.manifest:
                    self.manifest.count("skipped_duplicate")
                continue

            voucher = ap.get_voucher(self.paths, event["voucherId"])
            if voucher is None:
                ap.mark_event(self.paths, event["eventId"], "dlq")
                dlq.append(event["eventId"])
                if self.manifest:
                    self.manifest.count("dlq")
                    self.manifest.add_error(event["eventId"],
                                            f"voucher {event['voucherId']} not found")
                continue

            result = deliver(
                event,
                lambda v=voucher: self.process_voucher(v),
                dlq_path=self.paths.dlq / "ap_events.jsonl",
                max_attempts=self.max_attempts,
                backoff_s=self.backoff_s,
            )
            if self.manifest:
                self.manifest.attempts_total += result.attempts

            if not result.ok:
                ap.mark_event(self.paths, event["eventId"], "dlq")
                dlq.append(event["eventId"])
                if self.manifest:
                    self.manifest.count("dlq")
                    self.manifest.add_error(event["eventId"], result.error or "unknown")
                continue

            outcome = result.result["outcome"]
            ap.mark_event(self.paths, event["eventId"],
                          "blocked" if outcome == "BLOCKED" else "processed")
            if self.idem is not None:
                self.idem.put(idem_key, result.result)
            if outcome == "BLOCKED":
                blocked.append({"voucherId": result.result["voucherId"],
                                "code": result.result["matchStatus"]})
                if self.manifest:
                    self.manifest.count("blocked")
            else:
                posted.append(event["eventId"])
                if self.manifest:
                    self.manifest.count("posted")

        return {
            "posted": posted,
            "blocked": blocked,
            "skipped": skipped,
            "dlq": dlq,
            "pendingRemaining": sum(
                1 for e in ap.load_outbox(self.paths) if e["status"] == "pending"
            ),
        }
