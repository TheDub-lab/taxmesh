"""Deterministic synthetic-data generator for the taxmesh workspace.

run_seed() builds every source system from config.json + a fixed RNG seed,
so the whole platform is reproducible and tests can inject known breaks.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path

from ..config import Paths, get_paths, load_config
from ..util import robust_rmtree
from . import billing, crm, erp, tax_mart

ANCHOR_DATE = date(2026, 9, 30)

CUSTOMER_NAMES = [
    "Northstar Labs", "Cascade Retail", "Bluebird Media", "Halcyon Health",
    "Ironwood Robotics", "Juniper Analytics", "Kestrel Logistics", "Lumen Finance",
    "Meridian Games", "Nimbus Education", "Oakfield Grocery", "Quill Publishing",
    "Redwood Systems", "Tidewater Energy",
]

NEXUS_ZIPS = ["94105", "94103", "10001", "10011", "75201", "75202", "98101", "98104"]
NON_NEXUS_ZIPS = ["80202", "60601"]

TAX_CODE_WEIGHTS = [
    ("SAAS-SUB", 40), ("ELEC-DL", 15), ("HARDWARE", 15),
    ("PROF-SVC", 15), ("SHIP", 15),
]
CODE_DESCRIPTIONS = {
    "SAAS-SUB": "Platform subscription",
    "ELEC-DL": "Digital content download",
    "HARDWARE": "Peripheral device",
    "PROF-SVC": "Implementation services",
    "SHIP": "Ground shipping",
}

CERTS = [
    {"certId": "CERT-CA-1001", "customerCode": "CUST-002", "certType": "RESALE",
     "coversStates": ["US-CA"], "validFrom": "2026-01-01", "validTo": "2026-12-31"},
    {"certId": "CERT-CO-1002", "customerCode": "CUST-005", "certType": "RESALE",
     "coversStates": ["US-CA", "US-NY"], "validFrom": "2026-01-01", "validTo": "2026-12-31"},
    {"certId": "CERT-WA-1003", "customerCode": "CUST-008", "certType": "RESALE",
     "coversStates": ["US-WA"], "validFrom": "2025-01-01", "validTo": "2026-06-30"},
]

CODE_POOL = [
    ("SAAS-SUB", lambda rng: float(rng.choice([99.0, 249.0, 499.0, 1999.0, 4999.0])), 1),
    ("ELEC-DL", lambda rng: round(rng.uniform(19.0, 900.0), 2), 1),
    ("HARDWARE", lambda rng: round(rng.uniform(49.0, 650.0), 2), None),
    ("PROF-SVC", lambda rng: round(rng.uniform(500.0, 9500.0), 2), 1),
    ("SHIP", lambda rng: round(rng.uniform(9.0, 140.0), 2), 1),
]


def _weighted_code(rng: random.Random) -> str:
    draw = rng.randint(1, 100)
    upto = 0
    for code, weight in TAX_CODE_WEIGHTS:
        upto += weight
        if draw <= upto:
            return code
    return TAX_CODE_WEIGHTS[-1][0]


def _pick_zip(rng: random.Random) -> str:
    if rng.random() < 0.15:
        return rng.choice(NON_NEXUS_ZIPS)
    return rng.choice(NEXUS_ZIPS)


def _fresh_workspace(paths: Paths) -> None:
    for d in (paths.billing, paths.erp, paths.crm, paths.engine, paths.bus):
        robust_rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    for d in (paths.runs, paths.tieouts, paths.dlq, paths.flow):
        robust_rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    tax_mart.reset(paths)


def _build_customers(cfg: dict, rng: random.Random, count: int) -> list[dict]:
    customers = []
    for i in range(count):
        code = f"CUST-{i + 1:03d}"
        zip_code = _pick_zip(rng)
        state_code = cfg["jurisdictions"][cfg["zip_to_jurisdiction"][zip_code]]["state_code"]
        customers.append({
            "customerCode": code,
            "name": CUSTOMER_NAMES[i % len(CUSTOMER_NAMES)] if i < len(CUSTOMER_NAMES)
                    else f"{CUSTOMER_NAMES[i % len(CUSTOMER_NAMES)]} {i // len(CUSTOMER_NAMES) + 1}",
            "crmAccountId": f"CRM-{i + 1:03d}",
            "shipToZip": zip_code,
            "stateCode": state_code,
            "withholdingApplicable": rng.random() < 0.2,
        })
    # deterministic: CUST-004 always carries the withholding scenario so demos
    # and tests always exercise the withholding path
    if len(customers) >= 4:
        customers[3]["withholdingApplicable"] = True
    return customers


def _build_invoices(cfg: dict, rng: random.Random, customers: list[dict],
                    invoice_count: int, history_days: int) -> list[dict]:
    invoices = []
    code_pool_weights = []
    for code, _fn, _qty in CODE_POOL:
        weight = dict(TAX_CODE_WEIGHTS)[code]
        code_pool_weights.extend([code] * weight)

    for n in range(1, invoice_count + 1):
        cust = rng.choice(customers)
        # deterministic: INV-0002 always belongs to the withholding-flagged customer
        if n == 2 and customers:
            cust = next((c for c in customers if c["withholdingApplicable"]), cust)
        line_count = rng.randint(1, 4)
        lines = []
        for li in range(1, line_count + 1):
            code = rng.choice(code_pool_weights)
            fn = next(f for c, f, _ in CODE_POOL if c == code)
            qty_fixed = next(q for c, _, q in CODE_POOL if c == code)
            unit_price = fn(rng)
            quantity = qty_fixed if qty_fixed is not None else rng.randint(1, 5)
            lines.append({
                "lineId": str(li),
                "description": CODE_DESCRIPTIONS[code],
                "taxCode": code,
                "quantity": quantity,
                "unitPrice": unit_price,
                "amount": round(unit_price * quantity, 2),
            })

        ship_zip = cust["shipToZip"]
        if rng.random() < 0.2:
            ship_zip = _pick_zip(rng)

        issue_date = ANCHOR_DATE - timedelta(days=rng.randint(0, history_days - 1))
        subtotal = round(sum(l["amount"] for l in lines), 2)
        invoices.append({
            "invoiceId": f"INV-{n:04d}",
            "customerCode": cust["customerCode"],
            "issueDate": issue_date.isoformat(),
            "status": "finalized",
            "shipToZip": ship_zip,
            "subtotal": subtotal,
            "currency": cfg.get("currency", "USD"),
            "lines": lines,
            "tax": None,
        })
    return invoices


def run_seed(cfg: dict | None = None, root=None, inject: list[str] | None = None) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()
    inject = list(inject or [])
    rng = random.Random(cfg["seed"]["rng_seed"])

    _fresh_workspace(paths)

    customers = _build_customers(cfg, rng, cfg["seed"]["customers"])
    accounts = [
        {"crmAccountId": c["crmAccountId"], "customerCode": c["customerCode"],
         "name": c["name"], "segment": "commercial"} for c in customers
    ]
    invoices = _build_invoices(
        cfg, rng, customers, cfg["seed"]["invoices"], cfg["seed"]["history_days"]
    )

    events = [
        {
            "eventId": f"evt_{i + 1:04d}",
            "type": "invoice.finalized",
            "invoiceId": inv["invoiceId"],
            "createdAt": f"{inv['issueDate']}T09:00:00+00:00",
            "status": "pending",
        }
        for i, inv in enumerate(invoices)
    ]

    injected: list[dict] = []
    if "drop_event" in inject and invoices:
        victim = invoices[min(10, len(invoices) - 1)]["invoiceId"]
        events = [e for e in events if e["invoiceId"] != victim]
        injected.append({"break": "drop_event", "invoiceId": victim,
                         "effect": "billing event never reaches the tax engine"})
    if "dlq_event" in inject:
        poison = {
            "invoiceId": "INV-DLQ-01",
            "customerCode": "CUST-001",
            "issueDate": ANCHOR_DATE.isoformat(),
            "status": "finalized",
            "shipToZip": "00099",
            "subtotal": 100.0,
            "currency": cfg.get("currency", "USD"),
            "lines": [{"lineId": "1", "description": "Platform subscription",
                       "taxCode": "SAAS-SUB", "quantity": 1, "unitPrice": 100.0,
                       "amount": 100.0}],
            "tax": None,
        }
        invoices.append(poison)
        events.append({
            "eventId": "evt_dlq_0001",
            "type": "invoice.finalized",
            "invoiceId": poison["invoiceId"],
            "createdAt": f"{poison['issueDate']}T09:00:00+00:00",
            "status": "pending",
        })
        injected.append({"break": "dlq_event", "invoiceId": poison["invoiceId"],
                         "effect": "unmapped ship-to zip -> engine 422 -> DLQ after retries"})

    billing.save_invoices(paths, invoices)
    billing.save_outbox(paths, events)
    with open(paths.erp / "customers.json", "w", encoding="utf-8") as fh:
        json.dump(customers, fh, indent=2)
    with open(paths.erp / "journal.json", "w", encoding="utf-8") as fh:
        json.dump([], fh)
    with open(paths.erp / "items.json", "w", encoding="utf-8") as fh:
        json.dump([{"sku": f"SKU-{c}", "taxCode": c} for c, _, _ in CODE_POOL], fh, indent=2)
    with open(paths.crm / "accounts.json", "w", encoding="utf-8") as fh:
        json.dump(accounts, fh, indent=2)
    crm.write_certificates(paths, CERTS)
    with open(paths.engine / "certs.json", "w", encoding="utf-8") as fh:
        json.dump(CERTS, fh, indent=2)
    with open(paths.engine / "transactions.json", "w", encoding="utf-8") as fh:
        json.dump({"next_seq": 1, "items": []}, fh)
    with open(paths.bus / "idempotency.json", "w", encoding="utf-8") as fh:
        json.dump({}, fh)

    tax_mart.reset(paths)
    tax_mart.upsert_customers(paths, customers)
    tax_mart.upsert_jurisdictions(paths, cfg)
    paths.landing.mkdir(parents=True, exist_ok=True)
    (paths.landing / "epm").mkdir(parents=True, exist_ok=True)

    return {
        "anchorDate": ANCHOR_DATE.isoformat(),
        "workspaceRoot": str(paths.root),
        "customers": len(customers),
        "certificates": len(CERTS),
        "invoices": len(invoices),
        "outboxEvents": len(events),
        "injected": injected,
        "rngSeed": cfg["seed"]["rng_seed"],
    }
