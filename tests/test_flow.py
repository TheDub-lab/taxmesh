"""End-to-end: seed -> revenue flow -> controls, plus injected-break detection."""

from __future__ import annotations

import json

from src.controls.tieouts import run_tieouts
from src.integrations.bus import read_dlq
from src.integrations.flow import run_flow
from src.sources import billing, erp, tax_mart
from src.sources.seed import run_seed


def test_clean_flow_processes_every_invoice(workspace, engine):
    summary = run_seed(cfg=workspace.cfg, root=workspace.root)
    n = summary["invoices"]

    result = run_flow(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert result["status"] == "COMPLETED"
    assert len(result["processed"]) == n
    assert result["dlq"] == []

    invoices = billing.load_invoices(workspace.paths)
    assert all(inv.get("tax") is not None for inv in invoices)
    assert len(engine.transactions()) == n
    assert len(erp.load_journal(workspace.paths)) == n
    assert len(tax_mart.invoice_rows(workspace.paths)) == n

    tie = run_tieouts(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert tie["status"] == "PASS"
    assert sum(t["totalTax"] for t in engine.transactions()) > 0
    assert any(t["withholding"]["applies"] for t in engine.transactions())

    manifests = list(workspace.paths.runs.glob("revenue_flow-*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "COMPLETED"
    assert manifest["counts"]["processed"] == n


def test_second_run_is_idempotent(workspace, engine):
    run_seed(cfg=workspace.cfg, root=workspace.root)
    first = run_flow(cfg=workspace.cfg, root=workspace.root, engine=engine)
    txn_count = len(engine.transactions())

    # replaying an already-processed event must not create a second txn
    events = billing.load_outbox(workspace.paths)
    events[0]["status"] = "pending"
    billing.save_outbox(workspace.paths, events)

    second = run_flow(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert events[0]["eventId"] in second["skipped"]
    assert second["processed"] == []
    assert len(engine.transactions()) == txn_count
    assert first["processed"]


def test_dropped_event_is_caught_by_tie_outs(workspace, engine):
    summary = run_seed(cfg=workspace.cfg, root=workspace.root, inject=["drop_event"])
    victim = summary["injected"][0]["invoiceId"]

    result = run_flow(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert result["status"] == "COMPLETED"  # the flow itself cannot see the drop

    tie = run_tieouts(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert tie["status"] == "FAIL"
    assert victim in tie["exceptions"]["missingInEngine"]
    counts = {c["name"]: c for c in tie["controls"]}
    # a source-side drop is visible in every billing-anchored control...
    assert counts["count_billing_vs_engine"]["status"] == "FAIL"
    assert counts["count_billing_vs_engine"]["delta"] == 1  # billing 8 vs engine 7
    assert counts["count_billing_vs_gl"]["status"] == "FAIL"
    assert counts["count_billing_vs_mart"]["status"] == "FAIL"
    # ...while downstream engine<->GL consistency is intact: it was a drop, not a posting bug
    assert counts["money_tax_engine_vs_gl"]["status"] == "PASS"
    assert counts["money_withholding_engine_vs_gl"]["status"] == "PASS"


def test_poison_event_dead_letters_and_fails_controls(workspace, engine):
    summary = run_seed(cfg=workspace.cfg, root=workspace.root, inject=["dlq_event"])
    assert summary["injected"][0]["break"] == "dlq_event"

    result = run_flow(cfg=workspace.cfg, root=workspace.root, engine=engine,
                      max_attempts=3, backoff_s=0.0)
    assert result["status"] == "COMPLETED_WITH_EXCEPTIONS"
    assert result["dlq"] == ["evt_dlq_0001"]

    records = read_dlq(workspace.paths.dlq / "events.jsonl")
    assert len(records) == 1
    assert "ZIP_NOT_MAPPED" in records[0]["error"]
    assert records[0]["event"]["invoiceId"] == "INV-DLQ-01"

    outbox = {e["eventId"]: e for e in billing.load_outbox(workspace.paths)}
    assert outbox["evt_dlq_0001"]["status"] == "dlq"

    tie = run_tieouts(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert tie["status"] == "FAIL"
    assert tie["exceptions"]["dlqEvents"] == ["evt_dlq_0001"]


def test_drop_and_poison_together_keep_event_ids_unique(workspace, engine):
    summary = run_seed(cfg=workspace.cfg, root=workspace.root,
                       inject=["drop_event", "dlq_event"])
    victim = summary["injected"][0]["invoiceId"]

    ids = [e["eventId"] for e in billing.load_outbox(workspace.paths)]
    assert len(ids) == len(set(ids))  # a dropped event must not free an id for reuse

    result = run_flow(cfg=workspace.cfg, root=workspace.root, engine=engine,
                      max_attempts=3, backoff_s=0.0)
    assert result["status"] == "COMPLETED_WITH_EXCEPTIONS"
    assert result["dlq"] == ["evt_dlq_0001"]

    tie = run_tieouts(cfg=workspace.cfg, root=workspace.root, engine=engine)
    assert tie["status"] == "FAIL"
    assert victim in tie["exceptions"]["missingInEngine"]
    assert tie["exceptions"]["dlqEvents"] == ["evt_dlq_0001"]
