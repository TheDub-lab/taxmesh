# taxmesh

A small, working **tax-systems integration** platform with two end-to-end flows:
**revenue** (order-to-cash) and **procure-to-pay**. Synthetic billing, ERP, CRM,
procurement and AP systems feed a vendor-style **tax engine**; the engine's
results post to a general ledger, load into a tax mart, and are then **tied out
across sources** by automated controls that name any exception they find. The AP
side adds three-way match, self-assessed use tax, e-invoicing, a legacy
parallel run, and a go-live gate. Finally the mart is exported to a downstream
**EPM** landing zone and the run evidence is rolled up into a **health record**.

Everything is deterministic (`rng_seed: 42` for revenue, `1042` for AP): the same
seeds produce the same invoices, the same taxes, and the same control results,
every time.

```
Payline Billing ──invoice.finalized──► Atlas Tax Engine ──tax──┬─► Fincore ERP (GL)
      (outbox)         retry/DLQ        (deterministic)        │
Fincore Procure ──PO/receipts──────────────────────────────────┼─► Tax mart (SQLite)
Payline AP ──vendor_invoice.received──► (match → tax) ─────────┘        │
      (outbox + e-invoicing inbox)                                     ▼
Relate CRM (certs) ──────────────────────────────────────────►  Landing export (Planwell EPM)
          Controls: counts · money · hashes · per-jurisdiction  + AP match/coverage
          Parallel run (new vs legacy AP) ──► Go-live gate ──► Monitoring (manifests + DLQ)
```

> All rates, jurisdictions, nexus and account numbers are **synthetic and
> illustrative**. This is an integration, reconciliation and controls exercise —
> not tax advice and not real jurisdiction data.

---

## Two ways to read this repository

### 1. If you are evaluating *how* it was built

This project was built with an AI coding assistant, deliberately and openly, and
[`BUILD_LOG.md`](BUILD_LOG.md) is the unedited record of that: what was built,
what was *verified by running it*, and the real bugs that surfaced along the
way — a `Decimal`/`float` mismatch, SQLite handles left open on Windows, a
primary-key collision, an event-id collision between injected breaks, cloud
storage (OneDrive) refusing to delete directories that had become reparse points,
an AP double-entry that balanced only by accident (the input-tax allocation summed
engine rows for vendor-charged tax), a parallel run that could never flag its
signature variance (it compared AP credit, where self-assessed use tax never
appears), and a break-mode demo step that exited non-zero on a *successful*
run-with-exceptions.

The honest summary: the assistant wrote the code; I set the scope, reviewed the
diffs, ran the demos, and drove each failure to a fix — and where a test failed
for the wrong reason, the fix went into the code, not the assertion.

### 2. If you are hiring for a tax-systems role

| Tax-systems concern | Where it lives here |
| --- | --- |
| Tax engine integration & config | `src/engine/` — vendor-shaped API (`/v2/estimate`, `/v2/commit`, `/v2/nexus`, `/v2/certs/validate`), tax-code taxability, jurisdictional rates |
| Indirect tax determination | `src/engine/rules.py` — state/county/city components, shipping and services taxability, exemption certificates |
| **Procure-to-pay / AP tax** | `src/sources/ap.py` (three-way match, input-vs-use tax split), `src/sources/procurement.py`, `src/integrations/ap_connector.py` |
| **Self-assessed use tax** | `src/sources/ap.py::compute_voucher_tax` — vendor-charged tax vs self-assessed use tax, recoverable vs non-recoverable, posted to distinct GL accounts |
| **E-invoicing** | `src/sources/einvoicing.py` — reduced UBL-shaped XML render/parse round trip; inbox ingestion with per-file error isolation |
| Transaction posting to GL | `src/integrations/` + `src/sources/erp.py` — balanced double entry (AR 1200 / Revenue 4000 / Tax payable 2100 / Withholding 2105; AP 2110 / input tax 1300 / use tax 2200 / expense 5000 / non-recoverable 6100), idempotent by entry id |
| Reconciliation & tie-outs | `src/controls/tieouts.py` and `src/controls/ap_controls.py` — 9 revenue + 9 AP source-to-target controls, each with a per-jurisdiction engine-vs-GL table |
| **Migration parallel run** | `src/controls/parallel_run.py` + `src/sources/legacy_ap.py` — new AP pipeline vs the legacy process, with each variance *classified* (expected vs unexpected) |
| **Go-live readiness** | `src/controls/go_live.py` — gates the migration on control results, parallel-run variances, e-invoice coverage and the DLQ; exits non-zero when not ready |
| Exception handling | retry/backoff, dead letter queue (`outputs/dlq/`), idempotency store, named exceptions; match exceptions are business outcomes, transport errors are delivery failures |
| Data pipelines | billing/vendor invoices → engine → GL → tax mart (SQLite), plus a file landing zone for EPM |
| **Downstream integration (EPM)** | `src/exports/epm.py` — deterministic CSV extracts from the mart with a manifest recording row counts and content hashes |
| **Monitoring & observability** | `src/monitoring/health.py` — roll up run manifests and dead-letter queues into a HEALTHY/DEGRADED record; `run_monitor.py` exits non-zero when degraded |
| Auditability & evidence | run manifests with counts and content hashes (`outputs/runs/`) and written reports (`outputs/tieouts/`, `outputs/ap_tieouts/`, `outputs/parallel_run/`, `outputs/go_live/`, `landing/epm/`, `outputs/monitoring/`) |
| Testing & reproducibility | 67 tests over the engine API, the bus, both flows, matching, e-invoicing, the controls, the parallel run, the go-live gate, the EPM export and monitoring; seeded, deterministic data |
| Pros/cons and trade-offs | see "The controls" and "Known breaks" below — the platform is designed to *fail loudly and correctly*, not just to pass |

---

## Run it

Requires Python 3.10+ (developed on 3.14). Create a venv and install the four
dependencies in `requirements.txt`:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

One command runs the whole demo (seed → engine → flow → controls, both domains):

```powershell
.\run_demo.ps1                 # revenue + procure-to-pay: every control/gate PASSes
.\run_demo.ps1 -Phase ap       # procure-to-pay only
.\run_demo.ps1 -Phase ar       # revenue (order-to-cash) only
.\run_demo.ps1 -Breaks         # inject known breaks: controls FAIL and name them
```

Or step through revenue by hand:

```powershell
.venv\Scripts\python.exe run_seed.py            # synthetic source data
.venv\Scripts\python.exe run_engine.py          # start the tax engine (port 8077)
.venv\Scripts\python.exe run_revenue_flow.py    # drain events -> engine -> GL -> mart
.venv\Scripts\python.exe run_controls.py        # tie-outs; exit 1 if any control fails
```

And through procure-to-pay (the engine must be running):

```powershell
.venv\Scripts\python.exe run_seed_ap.py --reset     # suppliers, POs, receipts, vendor invoices (+XML)
.venv\Scripts\python.exe run_ap_flow.py            # match -> input/use tax -> GL -> subledger -> mart
.venv\Scripts\python.exe run_ap_controls.py        # AP tie-outs; exit 1 if any control fails
.venv\Scripts\python.exe run_parallel_run.py       # new pipeline vs the legacy AP process
.venv\Scripts\python.exe run_go_live.py            # go-live gate; exit 0 only when READY
```

And the hand-off and health steps (run after any of the flows):

```powershell
.venv\Scripts\python.exe run_export_epm.py         # tax mart -> deterministic CSV extracts for EPM
.venv\Scripts\python.exe run_monitor.py            # manifests + DLQ roll-up; exit 1 if DEGRADED
```

`run_controls.py`, `run_ap_controls.py`, `run_parallel_run.py`, `run_go_live.py`
and `run_monitor.py` exit non-zero when their verdict is not a pass, so they wire
straight into a scheduler or CI gate. `run_seed_ap.py --reset` clears the shared
engine/GL state so an AP-only run does not reconcile against a previous run's
leftovers.

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

### AP controls (`run_ap_controls.py`)

| Control | Kind | Left vs right |
| --- | --- | --- |
| `count_posted_subledger_vs_gl` | count | posted vouchers vs GL AP entries |
| `count_posted_vs_mart` | count | posted vouchers vs mart rows |
| `count_posted_vs_engine` | count | posted vouchers vs engine purchase transactions |
| `money_subledger_vs_gl_ap_credit` | money | subledger total vs GL AP credit |
| `money_recoverable_vs_gl_input_tax` | money | recoverable input tax vs GL 1300 debits |
| `money_selfassessed_vs_gl_use_tax` | money | self-assessed use tax vs GL 2200 credits |
| `hash_voucher_ids_subledger_vs_gl` | hash | content hash of voucher ids on both sides |
| `einvoice_parse_coverage` | count | XML files in the inbox vs files parsed without error |
| `blocked_vouchers_not_posted` | count | blocked vouchers that *did* reach the GL (must be 0) |

Plus a **per-jurisdiction** table tying engine self-assessed use tax to GL 2200.
Note the distinction the AP controls make explicit: a *match failure* blocks a
voucher but leaves every count and money tie-out internally consistent (the
exception is named, not hidden), whereas a *posting or transport error* breaks a
tie-out and/or produces a dead letter.

## Downstream hand-off and health

**EPM export** (`run_export_epm.py`, `src/exports/epm.py`) writes four flat
extracts from the mart — `dim_jurisdictions`, `fct_invoices`,
`fct_vendor_invoices`, and a combined `jurisdiction_tax` (AR sales tax + AP
self-assessed use tax) — into `landing/epm/`, alongside a manifest recording each
file's row count and content hash. Extracts are deterministic: sorted rows, fixed
line endings, volatile columns excluded, so an unchanged mart re-exports
byte-identically (there is a test for exactly that).

**Monitoring** (`run_monitor.py`, `src/monitoring/health.py`) reads every run
manifest and dead-letter queue back and produces one health record: run counts by
status, any degraded (non-`COMPLETED`) runs, and dead-letter totals. The CLI
exits non-zero when the platform is `DEGRADED`, which happens whenever a run did
not finish cleanly or anything is sitting in a DLQ.

## Known breaks (fail-safe demo)

Revenue — `run_seed.py --inject drop_event,dlq_event` injects two faults:

- **`drop_event`** — a billing event is never delivered, so one invoice silently
  misses the engine, GL and mart.
- **`dlq_event`** — an invoice ships to an unmapped postal code; the engine
  rejects it (`422`), retries are exhausted, and the event is dead-lettered.

AP — `run_seed_ap.py --inject price_variance,no_po,duplicate_invoice` injects
three realistic supplier faults:

- **`price_variance`** — an invoice is priced above the PO → `PRICE_VARIANCE`.
- **`no_po`** — an invoice references no purchase order → `NO_PO`.
- **`duplicate_invoice`** — two vouchers share a supplier invoice number → the
  *later* one is blocked as `DUPLICATE`.

…and `run_parallel_run.py --inject legacy_drift` (also wired into
`run_demo.ps1 -Breaks` for AP) adds a $100 error to a legacy posting to prove the
parallel run surfaces an **unexpected** variance.

The point is the *response*: the flow still reports a status, bad data is blocked
before it reaches the GL, the DLQ catches poison events, the parallel run
classifies every difference, and the controls fail loudly while naming exactly
what went missing — while the posting tie-outs still tie, showing the break is
upstream rather than a posting error. The go-live gate then returns `NOT_READY`.

---

## Project layout

```
src/
  config.py            config + path resolution (see "Where the data goes")
  util.py              money/time helpers, atomic + retry-tolerant file IO
  engine/              Atlas tax engine: app.py (FastAPI), rules.py, store.py
  sources/             billing.py, erp.py, crm.py, tax_mart.py, seed.py,
                       procurement.py, ap.py, einvoicing.py, legacy_ap.py, seed_ap.py
  integrations/        bus.py, engine_client.py, billing_connector.py, flow.py,
                       ap_connector.py, ap_flow.py
  controls/            tieouts.py, ap_controls.py, parallel_run.py, go_live.py
  exports/epm.py       tax mart -> EPM landing extracts
  monitoring/health.py run-manifest + DLQ health roll-up
tests/                 pytest suite
run_*.py               CLI entry points (revenue, AP, export, monitoring)
run_demo.ps1           one-command demo (-Phase ar|ap|all, -Breaks)
```

## Testing

```powershell
.venv\Scripts\python.exe -m pytest
```

67 tests cover the determination rules, the HTTP API, retry/DLQ/idempotency, both
end-to-end flows, three-way match, the input/use-tax split, e-invoicing round
trip, the revenue and AP tie-outs, the parallel run, the go-live gate, the EPM
export (including determinism) and the monitoring roll-up. No network access is
required.
