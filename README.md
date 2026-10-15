# taxmesh

A small, working **tax-systems integration** platform: synthetic billing, ERP
and CRM systems feed a vendor-style **tax engine**, the engine's results are
posted to a general ledger, loaded into a tax mart, and then **tied out across
sources** by automated controls that name any exception they find.

Everything is deterministic (`rng_seed: 42`): the same seed produces the same
invoices, the same taxes, and the same control results, every time.

```
Payline Billing ──invoice.finalized──► Atlas Tax Engine ──tax──┬─► Fincore ERP (GL)
      (outbox)         retry/DLQ        (deterministic)        │
                                                               ├─► Tax mart (SQLite)
Relate CRM (certs) ──────────────────────────────────────────► │
                                                               ▼
                               Controls: counts · money · hashes · per-jurisdiction
```

> All rates, jurisdictions, nexus and account numbers are **synthetic and
> illustrative**. This is an integration, reconciliation and controls exercise —
> not tax advice and not real jurisdiction data.

---

## Two ways to read this repository

### 1. If you are evaluating *how* it was built

This project was built with an AI coding assistant, deliberately and openly, and
[`BUILD_LOG.md`](BUILD_LOG.md) is the unedited record of that: what was built,
what was *verified by running it*, and the five real bugs that surfaced along the
way — a `Decimal`/`float` mismatch, SQLite handles left open on Windows, a
primary-key collision, an event-id collision between injected breaks, and cloud
storage (OneDrive) refusing to delete directories that had become reparse
points.

The honest summary: the assistant wrote the code; I set the scope, reviewed the
diffs, ran the demos, and drove each failure to a fix — and where a test failed
for the wrong reason, the fix went into the code, not the assertion.

### 2. If you are hiring for a tax-systems role

| Tax-systems concern | Where it lives here |
| --- | --- |
| Tax engine integration & config | `src/engine/` — vendor-shaped API (`/v2/estimate`, `/v2/commit`, `/v2/nexus`, `/v2/certs/validate`), tax-code taxability, jurisdictional rates |
| Indirect tax determination | `src/engine/rules.py` — state/county/city components, shipping and services taxability, exemption certificates |
| Transaction posting to GL | `src/integrations/` + `src/sources/erp.py` — balanced double entry (AR 1200 / Revenue 4000 / Tax payable 2100 / Withholding 2105), idempotent by entry id |
| Reconciliation & tie-outs | `src/controls/tieouts.py` — 9 source-to-target controls + a per-jurisdiction engine-vs-GL table |
| Exception handling | retry/backoff, dead letter queue (`outputs/dlq/events.jsonl`), idempotency store, named exceptions |
| Data pipelines | billing → engine → GL → tax mart (SQLite), plus a file landing zone for EPM |
| Auditability & evidence | run manifests with counts and content hashes (`outputs/runs/`) and written control reports (`outputs/tieouts/`) |
| Testing & reproducibility | 30 tests over the engine API, the bus, the flow and the controls; seeded, deterministic data |
| Pros/cons and trade-offs | see "The controls" and "Known breaks" below — the platform is designed to *fail loudly and correctly*, not just to pass |

---

## Run it

Requires Python 3.10+ (developed on 3.14). Create a venv and install the four
dependencies in `requirements.txt`:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

One command runs the whole demo (seed → engine → flow → controls):

```powershell
.\run_demo.ps1              # clean run: every control PASSes
.\run_demo.ps1 -Breaks      # inject known breaks: controls FAIL and name them
```

Or step through it by hand:

```powershell
.venv\Scripts\python.exe run_seed.py            # synthetic source data
.venv\Scripts\python.exe run_engine.py          # start the tax engine (port 8077)
.venv\Scripts\python.exe run_revenue_flow.py    # drain events -> engine -> GL -> mart
.venv\Scripts\python.exe run_controls.py        # tie-outs; exit 1 if any control fails
```

`run_controls.py` exits non-zero when any control fails, so it can be wired
straight into a scheduler or CI gate.

### Where the data goes

Runtime files (`data/`, `outputs/`, `landing/`) are scratch and are gitignored.
By default they are written under the repository root. If the repository lives
inside a cloud-synced folder (OneDrive), the workspace is automatically
redirected to `%LOCALAPPDATA%\taxmesh` instead, because sync clients hold
transient locks that break atomic writes. Override the location with the
`TAXMESH_ROOT` environment variable, or pass `--root <path>` to any entry point.

---

## The controls

Every control compares two independently extracted views of the same truth.
Anything non-zero is an exception with a named variance.

| Control | Kind | Left vs right |
| --- | --- | --- |
| `count_billing_vs_engine` | count | invoices vs engine transactions |
| `count_billing_vs_gl` | count | invoices vs GL entries |
| `count_billing_vs_mart` | count | invoices vs mart rows |
| `money_subtotal_billing_vs_engine` | money | invoiced subtotal vs engine subtotal |
| `money_subtotal_billing_vs_gl` | money | invoiced subtotal vs GL revenue |
| `money_tax_engine_vs_billing` | money | engine tax vs billing-recorded tax |
| `money_tax_engine_vs_gl` | money | engine tax vs GL tax payable |
| `money_withholding_engine_vs_gl` | money | engine withholding vs GL withholding |
| `hash_invoice_ids_billing_vs_engine` | hash | content hash of invoice ids on both sides |

Plus a **per-jurisdiction** table tying engine tax to GL tax for each
jurisdiction (US-CA-SF, US-NY-NYC, US-TX-DAL, US-WA-KING).

## Known breaks (fail-safe demo)

`run_seed.py --inject drop_event,dlq_event` injects two realistic faults:

- **`drop_event`** — a billing event is never delivered, so one invoice silently
  misses the engine, GL and mart.
- **`dlq_event`** — an invoice ships to an unmapped postal code; the engine
  rejects it (`422`), retries are exhausted, and the event is dead-lettered.

The point is the *response*: the flow still reports a status, the DLQ catches the
poison event, and the controls fail loudly while naming exactly what went missing
— while engine-vs-GL tax still ties, showing the break is upstream rather than a
posting error.

---

## Project layout

```
src/
  config.py            config + path resolution (see "Where the data goes")
  util.py              money/time helpers, atomic + retry-tolerant file IO
  engine/              Atlas tax engine: app.py (FastAPI), rules.py, store.py
  sources/             billing.py, erp.py, crm.py, tax_mart.py, seed.py
  integrations/        bus.py, engine_client.py, billing_connector.py, flow.py
  controls/tieouts.py  the controls
tests/                 pytest suite
run_*.py               CLI entry points
run_demo.ps1           one-command demo
```

## Testing

```powershell
.venv\Scripts\python.exe -m pytest
```

30 tests cover the determination rules, the HTTP API, retry/DLQ/idempotency, the
end-to-end flow, and the tie-outs. No network access is required.
