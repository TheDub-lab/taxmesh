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
