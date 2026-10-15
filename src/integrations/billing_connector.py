"""Billing -> tax engine -> GL -> mart connector (the revenue-flow spine).

process_event is idempotent at every step so at-least-once bus delivery is safe:
  1. estimate + commit at the engine (engine commit is idempotent by docId)
  2. record tax lines back onto the billing invoice
  3. post the double-entry in the ERP GL (idempotent by entry id)
  4. load the invoice facts into the tax mart (delete + insert)
"""

from __future__ import annotations

from ..config import Paths
from ..sources import billing, erp, tax_mart
from .bus import IdempotencyStore, RunManifest, deliver
from .engine_client import EngineClient


class ConnectorError(Exception):
    pass


class BillingConnector:
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

    def process_event(self, event: dict) -> dict:
        invoice_id = event["invoiceId"]
        invoice = billing.get_invoice(self.paths, invoice_id)
        if invoice is None:
            raise ConnectorError(f"invoice {invoice_id} not found in billing store")
        if invoice["status"] != "finalized":
            raise ConnectorError(f"invoice {invoice_id} has status {invoice['status']}")

        if invoice.get("tax") is None:
            customer = erp.find_customer(self.paths, invoice["customerCode"])
            payload = billing.build_estimate_request(self.cfg, invoice, customer)
            estimate = self.engine.estimate(payload)
            txn = self.engine.commit(payload)
            invoice = billing.record_tax(self.paths, invoice_id, estimate, txn["txnId"])
            if invoice is None:
                raise ConnectorError(f"invoice {invoice_id} vanished during processing")

        tax_result = invoice["tax"]
        entry = erp.post_invoice_entry(self.cfg, self.paths, invoice, tax_result)
        tax_mart.load_invoice(self.paths, invoice, tax_result, entry)

        return {
            "invoiceId": invoice_id,
            "txnId": tax_result["engineTxnId"],
            "entryId": entry["entryId"],
            "totalTax": tax_result["totalTax"],
            "withholding": tax_result["withholding"]["amount"],
        }

    def run(self) -> dict:
        events = sorted(
            (e for e in billing.load_outbox(self.paths) if e["status"] == "pending"),
            key=lambda e: e["eventId"],
        )
        processed, skipped, dlq = [], [], []

        for event in events:
            idem_key = f"evt:{event['eventId']}"
            if self.idem is not None and self.idem.get(idem_key) is not None:
                billing.mark_event(self.paths, event["eventId"], "processed")
                skipped.append(event["eventId"])
                if self.manifest:
                    self.manifest.count("skipped_duplicate")
                continue

            result = deliver(
                event,
                lambda ev=event: self.process_event(ev),
                dlq_path=self.paths.dlq / "events.jsonl",
                max_attempts=self.max_attempts,
                backoff_s=self.backoff_s,
            )
            if self.manifest:
                self.manifest.attempts_total += result.attempts

            if result.ok:
                billing.mark_event(self.paths, event["eventId"], "processed")
                if self.idem is not None:
                    self.idem.put(idem_key, result.result)
                processed.append(event["eventId"])
                if self.manifest:
                    self.manifest.count("processed")
            else:
                billing.mark_event(self.paths, event["eventId"], "dlq")
                dlq.append(event["eventId"])
                if self.manifest:
                    self.manifest.count("dlq")
                    self.manifest.add_error(event["eventId"], result.error or "unknown")

        return {
            "processed": processed,
            "skipped": skipped,
            "dlq": dlq,
            "pendingRemaining": sum(
                1 for e in billing.load_outbox(self.paths) if e["status"] == "pending"
            ),
        }
