"""Revenue-flow orchestration: drain the billing outbox, then hash the results."""

from __future__ import annotations

import json

from ..config import Paths, get_paths, load_config
from ..sources import billing, erp, tax_mart
from .billing_connector import BillingConnector
from .bus import IdempotencyStore, RunManifest
from .engine_client import EngineClient


def run_flow(
    cfg: dict | None = None,
    root=None,
    engine: EngineClient | None = None,
    max_attempts: int = 3,
    backoff_s: float = 0.0,
) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()

    own_engine = engine is None
    if own_engine:
        engine = EngineClient(base_url=cfg["engine"]["base_url"])

    manifest = RunManifest("revenue_flow", paths, notes={
        "maxAttempts": max_attempts,
        "engine": "atlas-local",
    })
    idem = IdempotencyStore(paths.bus / "idempotency.json")
    connector = BillingConnector(
        cfg, paths, engine, manifest=manifest, idem=idem,
        max_attempts=max_attempts, backoff_s=backoff_s,
    )

    try:
        outbox = billing.load_outbox(paths)
        manifest.set_hash("outbox_event_ids", sorted(e["eventId"] for e in outbox))
        summary = connector.run()

        invoices = billing.load_invoices(paths)
        manifest.set_hash(
            "billing_finalized_ids",
            sorted(i["invoiceId"] for i in invoices if i["status"] == "finalized"),
        )
        manifest.set_hash(
            "gl_entry_ids", sorted(e["entryId"] for e in erp.load_journal(paths))
        )
        manifest.set_hash(
            "mart_invoice_ids", sorted(r["invoice_id"] for r in tax_mart.invoice_rows(paths))
        )

        status = "COMPLETED" if not summary["dlq"] else "COMPLETED_WITH_EXCEPTIONS"
        manifest_path = manifest.finish(status)

        result = {
            **summary,
            "runId": manifest.run_id,
            "manifestPath": str(manifest_path),
            "status": status,
        }
    finally:
        if own_engine:
            engine.close()

    out = paths.flow / "revenue_flow_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    return result
