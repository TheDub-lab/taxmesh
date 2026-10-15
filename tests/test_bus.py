"""Bus guarantees: retries, DLQ exhaustion, idempotency, run manifests."""

from __future__ import annotations

import json

from src.integrations.bus import IdempotencyStore, RunManifest, deliver, read_dlq


def _event(n=1):
    return {"eventId": f"evt_{n:04d}", "type": "invoice.finalized", "invoiceId": f"INV-{n:04d}"}


def test_deliver_success_first_attempt(tmp_path):
    result = deliver(_event(), lambda: {"ok": True},
                      dlq_path=tmp_path / "dlq.jsonl")
    assert result.ok and result.attempts == 1 and result.dlq is False
    assert result.result == {"ok": True}
    assert not (tmp_path / "dlq.jsonl").exists()


def test_deliver_retries_transient_failure(tmp_path):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise IOError("transient network error")
        return "recovered"

    result = deliver(_event(), flaky, dlq_path=tmp_path / "dlq.jsonl",
                     max_attempts=3, sleep=lambda _s: None)
    assert result.ok and result.attempts == 3
    assert result.result == "recovered"


def test_deliver_exhausts_to_dlq(tmp_path):
    def always_fails():
        raise ValueError("poison event")

    result = deliver(_event(7), always_fails, dlq_path=tmp_path / "dlq.jsonl",
                     max_attempts=3, sleep=lambda _s: None)
    assert result.ok is False and result.dlq is True and result.attempts == 3
    assert "poison event" in result.error

    records = read_dlq(tmp_path / "dlq.jsonl")
    assert len(records) == 1
    assert records[0]["eventId"] == "evt_0007"
    assert records[0]["event"]["invoiceId"] == "INV-0007"
    assert records[0]["attempts"] == 3


def test_idempotency_store_roundtrip(tmp_path):
    store = IdempotencyStore(tmp_path / "idem.json")
    assert store.get("evt:1") is None
    store.put("evt:1", {"txnId": "TXN-000001"})
    assert store.get("evt:1") == {"txnId": "TXN-000001"}
    # a second instance sees the same persisted state
    fresh = IdempotencyStore(tmp_path / "idem.json")
    assert fresh.get("evt:1")["txnId"] == "TXN-000001"


def test_run_manifest_writes_counts_hashes_and_status(workspace):
    manifest = RunManifest("unit_job", workspace.paths, notes={"k": "v"})
    manifest.count("processed", 3)
    manifest.count("dlq")
    manifest.attempts_total = 5
    manifest.add_error("evt_0001", "boom")
    manifest.set_hash("invoice_ids", ["INV-1", "INV-2"])
    path = manifest.finish("COMPLETED_WITH_EXCEPTIONS")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "COMPLETED_WITH_EXCEPTIONS"
    assert payload["counts"] == {"processed": 3, "dlq": 1}
    assert payload["attemptsTotal"] == 5
    assert payload["errors"][0]["ref"] == "evt_0001"
    assert len(payload["hashes"]["invoice_ids"]) == 16
    assert payload["notes"] == {"k": "v"}
    assert payload["finishedAt"] is not None
