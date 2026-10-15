"""AP flow orchestration: drain the vendor-invoice outbox, then hash the results."""

from __future__ import annotations

import json

from ..config import get_paths, load_config
from ..sources import ap, erp, tax_mart
from .ap_connector import ApConnector
from .bus import IdempotencyStore, RunManifest
from .engine_client import EngineClient


def run_ap_flow(
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

    manifest = RunManifest("ap_flow", paths, notes={
        "maxAttempts": max_attempts,
        "engine": "atlas-local",
        "stream": "PURCHASE",
    })
    idem = IdempotencyStore(paths.ap / "idempotency.json")
    connector = ApConnector(
        cfg, paths, engine, manifest=manifest, idem=idem,
        max_attempts=max_attempts, backoff_s=backoff_s,
    )

    try:
        outbox = ap.load_outbox(paths)
        manifest.set_hash("ap_outbox_event_ids", sorted(e["eventId"] for e in outbox))
        summary = connector.run()

        manifest.set_hash("voucher_ids", sorted(v["voucherId"] for v in ap.load_vouchers(paths)))
        manifest.set_hash("subledger_voucher_ids",
                          sorted(r["voucherId"] for r in ap.load_subledger(paths)))
        manifest.set_hash("mart_voucher_ids",
                          sorted(r["voucher_id"] for r in tax_mart.vendor_invoice_rows(paths)))
        manifest.set_hash("gl_ap_entry_ids", sorted(
            e["entryId"] for e in erp.load_journal(paths) if e.get("voucherId")
        ))

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

    out = paths.flow / "ap_flow_summary.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    return result
