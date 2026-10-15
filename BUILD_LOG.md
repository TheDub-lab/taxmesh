# Build log

A running, honest record of how this repository was built, what broke, and how
each break was diagnosed. The point of this file is transparency: the code was
written with an AI assistant, and this is what that process actually looked like
— including the failures.

---

## Session 1 — 2026-10-15 — Phase 1: revenue flow, controls, demo

### What was built

A working "revenue-to-tax-to-GL" spine with cross-system controls:

- **Atlas tax engine** (`src/engine/`) — a FastAPI service that behaves like a
  tax vendor API: deterministic jurisdictional rates, tax-code taxability,
  exemption-certificate validation, idempotent commit by document id.
- **Synthetic source systems** (`src/sources/`) — Payline Billing (invoices +
  webhook outbox), Fincore ERP (GL journal), Relate CRM (customers, certificates),
  and a SQLite tax mart. Everything is generated from a fixed seed (`rng_seed: 42`)
  so every run is reproducible.
- **Integration layer** (`src/integrations/`) — at-least-once delivery with
  retry/backoff, a dead letter queue, per-step idempotency, and a run manifest
  that records counts and content hashes for each stage.
- **Controls** (`src/controls/tieouts.py`) — nine source-to-target tie-outs
  (counts, monetary totals, id hashes) plus a per-jurisdiction engine-vs-GL table.
- **CLI entry points** — `run_seed.py`, `run_engine.py`, `run_revenue_flow.py`,
  `run_controls.py`, and a one-command `run_demo.ps1`.
- **Test suite** — 30 tests (rules, engine HTTP API, bus/retry/DLQ, flow
  idempotency, tie-outs), run with `pytest`.

### Verified behaviour (not just claimed)

- Clean run: 120 invoices seeded, 120/120 processed, all nine controls **PASS**,
  all four jurisdictions tie engine-to-GL, withholding posts to GL 2105.
- Break run (`.\run_demo.ps1 -Breaks`): the flow still finishes, the poison
  event lands in the DLQ, and the controls **FAIL** while naming the exceptions
  (`INV-0011` missing in engine, `INV-DLQ-01` missing, `evt_dlq_0001`
  dead-lettered) — and engine-vs-GL tax still ties, proving the break is
  upstream, not a posting bug.

### Bugs found and fixed during this session

Each of these was caught by actually running the thing, not by inspection.

1. **Float × Decimal with withholding.** Withholding was computed as
   `subtotal * rate` where `subtotal` had been coerced to `float`, producing a
   `TypeError` on the flagged-customer path. Fixed by keeping the value in
   `Decimal` and rounding to cents after the multiply.
2. **SQLite handles left open on Windows.** Connections were not being closed,
   so seeding could not delete `tax_mart.sqlite` on the next run
   (`PermissionError`). Fixed by routing all access through a `_Session` helper
   that always commits and closes.
3. **Primary-key collision in `fct_tax_lines`.** A single invoice line can carry
   several tax components (state/county/city) that share a jurisdiction id, so
   the composite key collided. Fixed by adding `jurisdiction_type` to the key.
4. **Event-id collision between break injections.** The `dlq_event` poison
   event was numbered from the current outbox length. When `drop_event` ran
   first it removed an event, the poison event reused an existing id, and it was
   silently swallowed by the idempotency check (marked processed, never sent).
   The *combination* of the two breaks exposed this, which is exactly why the
   demo runs both together. Fixed by giving the poison event a stable, distinct
   id, and locked with a regression test that asserts event ids stay unique
   after a drop.
5. **Cloud-sync locks on the workspace.** The repository lives inside a OneDrive
   folder. OneDrive manages `data/` with reparse points and holds transient
   locks, so `os.replace` and `os.rmdir` intermittently failed with
   `[WinError 5] Access is denied` mid-run. Retrying was not enough — the
   directories were cloud placeholders. Fixed properly by resolving the runtime
   workspace *out* of synced folders (to `%LOCALAPPDATA%\taxmesh`, overridable
   with `TAXMESH_ROOT`), and by making every atomic write and directory removal
   retry-tolerant. This is an unglamorous integration problem that a demo which
   only "prints PASS" would never surface.

### Honest notes

- All tax rates, jurisdictions, nexus, and account numbers are synthetic and
  illustrative. This is an integration and controls exercise, not tax advice.
- The AI assistant wrote the code; I set the scope, reviewed the diff, ran the
  demos, and diagnosed/reported the failures above until the suite was green.
  Where a test failed for the wrong reason, the fix was applied to the code, not
  to the assertion.
- Timing/progress here reflects one working session; later phases append their
  own sections rather than rewriting this one.

---

## Session 2 — 2026-10-15 — Phase 2: procure-to-pay, parallel run, go-live gate

### What was built

A second end-to-end flow (procure-to-pay / AP) reusing the same tax engine, GL
and control machinery as revenue:

- **Procurement source** (`src/sources/procurement.py`) — suppliers, purchase
  orders and goods receipts.
- **AP source** (`src/sources/ap.py`) — vendor invoices, a delivery outbox, an AP
  subledger, and two pieces of domain logic: a **three-way match** (PO vs receipt
  vs invoice) and the **input-tax split** (vendor-charged vs self-assessed use
  tax, recoverable vs non-recoverable, per line).
- **E-invoicing** (`src/sources/einvoicing.py`) — a reduced UBL-shaped XML
  format with a render/parse round trip; an inbox is ingested with per-file error
  isolation so one bad file cannot stop the batch.
- **Legacy AP process** (`src/sources/legacy_ap.py`) — the "before" system being
  migrated away from: no match gate, vendor-charged tax only, banker's rounding.
  Its gaps are the *expected* variances the parallel run is built to surface.
- **AP connector/flow** (`src/integrations/ap_connector.py`, `ap_flow.py`) — the
  same retry/DLQ/idempotency spine as revenue, extended with blocked-vouchers as
  business outcomes; match exceptions do not retry, transport errors do.
- **AP controls** (`src/controls/ap_controls.py`) — nine tie-outs including the
  input-vs-use tax split, e-invoice parse coverage, and "blocked never posted".
  The GL tie-outs now filter by document type (`SALE` vs `PURCHASE`) so the two
  domains share one engine transaction log and one journal.
- **Parallel run** (`src/controls/parallel_run.py`) — classifies every legacy-vs-new
  difference as `OK`, `EXPECTED_MISSING_USE_TAX`, `EXPECTED_ROUNDING`,
  `EXPECTED_BLOCKED_UNMATCHED`, or `UNEXPECTED`.
- **Go-live gate** (`src/controls/go_live.py`) — turns control results,
  parallel-run variances, e-invoice coverage and the DLQ into a READY/NOT_READY
  gate; exits non-zero when not ready.
- **CLIs and demo** — `run_seed_ap.py`, `run_ap_flow.py`, `run_ap_controls.py`,
  `run_parallel_run.py`, `run_go_live.py`; `run_demo.ps1` gained
  `-Phase ar|ap|all` and break injection for both domains.
- **Tests** — 28 new tests (58 total) over matching, e-invoicing, the AP flow and
  idempotency, AP controls, the parallel run, and the go-live gate.

### Verified behaviour (not just claimed)

- Clean AP run: 60 vouchers seeded (15 as e-invoices), 60/60 posted, all nine AP
  controls **PASS**, all four jurisdictions tie engine-to-GL 2200. Totals:
  subledger/AP credit 260,409.98; recoverable input tax 9,737.04; self-assessed
  use tax 9,195.95.
- Parallel run: 60 compared, 32 OK, 28 `EXPECTED_MISSING_USE_TAX`, **0
  unexpected** → PASS.
- Go-live: **READY** across all five gates. Full `-Phase all` run (revenue + AP
  sharing the engine and journal) also READY.
- Break run (`.\run_demo.ps1 -Phase ap -Breaks`): three vouchers blocked with the
  names `VINV-00006(PRICE_VARIANCE)`, `VINV-00008(NO_PO)`, `VINV-00010(DUPLICATE)`;
  the AP controls stay internally consistent (the exception is named, not hidden);
  a legacy drift produces **1 unexpected** variance; the go-live gate returns
  **NOT_READY** and the script exits 0 because everything failed *as designed*.

### Bugs found and fixed during this session

Again, each was caught by running it, not by reading it.

1. **AP double-entry balanced only by accident.** For a vendor-charged line the
   input-tax debit was built by summing the *engine's* per-jurisdiction rows
   rather than the vendor-charged amount, so debits exceeded credits
   (`DR 1706.41 != CR 1694.33`) and four vouchers dead-lettered with a `ValueError`.
   The clean run surfaced it immediately. Fixed by allocating vendor-charged tax
   to the voucher jurisdiction and splitting only the self-assessed portion by the
   engine's rows — the two then balance exactly.
2. **The parallel run could never flag its signature variance.** It compared AP
   credit, but self-assessed use tax is credited to Use Tax Payable, *not* AP, so
   legacy and new AP credit matched for every voucher and the run reported
   all-OK (no signal at all). Fixed by comparing **total recognised cost** (all
   debits = subtotal + total tax), which differs from the legacy amount by exactly
   the self-assessed tax. The classification `EXPECTED_MISSING_USE_TAX` now fires
   for precisely the self-assessed vouchers, and a test ties that set equality.
3. **An injected drift could blend into an expected variance.** `legacy_drift`
   originally targeted a fixed posting index, which could land on a self-assessed
   voucher and turn an "expected" difference into an ambiguous one. Fixed to
   target a voucher the legacy process already agrees on, so the injected error
   shows as an unclassified `-100.00` variance.
4. **An AP-only demo reconciled against a previous run's leftovers.** The AP
   domain shares the engine transaction log and the GL journal with revenue, and a
   prior clean run left 60 sale transactions behind, so a break run then reported
   impossible counts (57 posted but 60 in the subledger) and failed controls for
   the wrong reason. Fixed with `run_seed_ap(reset=True)` (used by
   `run_demo.ps1 -Phase ap`), which clears the shared state before seeding, plus a
   regression test that seeds revenue, runs it, then asserts an AP reset leaves no
   sale transactions and an empty journal.

### Honest notes

- The self-assessed-use-tax story is illustrative but the mechanism is real:
  purchases where the supplier charges no tax still create a use-tax liability,
  recoverable (input tax credit) or not (e.g. meals & entertainment), and the GL
  accounts differ accordingly.
- The vendor-charged rate (`7.25%`) and all amounts are synthetic.
- The same working method applied as in Session 1: the assistant wrote the code;
  I scoped it, reviewed the diffs, ran both demos, and drove every failure above
  to a fix — and when a test failed for the wrong reason, the fix went into the
  code or the fixture, never into an assertion to make it pass.
