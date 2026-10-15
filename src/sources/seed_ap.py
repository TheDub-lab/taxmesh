"""Deterministic AP seed: suppliers, POs, receipts, vendor invoices (JSON + XML).

Mirrors src/sources/seed.py for the AR domain. Same anchor date, same idea:
one fixed rng seed reproduces the whole AP population, and named breaks can be
injected so the controls have something real to catch.
"""

from __future__ import annotations

import random
from datetime import timedelta

from ..config import Paths, get_paths, load_config
from ..util import atomic_write_json, robust_rmtree
from . import ap, einvoicing, procurement, tax_mart
from .seed import ANCHOR_DATE, _fresh_workspace

SUPPLIER_NAMES = [
    "Cascade Components", "Northern Light Supply", "Meridian Paper", "Ironclad Tools",
    "Vertex Electronics", "Harbor Freight Lines", "Bluepeak Catering", "Summit Facilities",
]

CODE_POOL_AP = [
    ("HARDWARE", "Tangible equipment and components"),
    ("PROF-SVC", "Professional services"),
    ("SAAS-SUB", "Software subscription"),
    ("ELEC-DL", "Electronic download"),
    ("FREIGHT-IN", "Inbound freight"),
    ("MEALS-ENT", "Client meals and entertainment"),
]

VENDOR_TAX_RATE = 0.0725  # illustrative vendor-charged rate for the demo


def _state_for_zip(cfg: dict, zip_code: str):
    jid = cfg["zip_to_jurisdiction"].get(str(zip_code))
    if jid is None:
        return None
    return cfg["jurisdictions"][jid]["state_code"]


def _build_suppliers(cfg: dict, rng: random.Random, count: int) -> list[dict]:
    zips = sorted(cfg["zip_to_jurisdiction"])
    suppliers = []
    for i in range(1, count + 1):
        zip_code = zips[(i * 3) % len(zips)]
        suppliers.append({
            "supplierCode": f"SUP-{i:03d}",
            "name": SUPPLIER_NAMES[(i - 1) % len(SUPPLIER_NAMES)],
            "taxId": f"TIN-{2000 + i}",
            "remitToZip": zip_code,
            "supplierZip": zip_code,
            "registeredStates": [cfg["jurisdictions"][cfg["zip_to_jurisdiction"][z]]["state_code"]
                                 for z in zips if rng.random() < 0.5] or ["US-CA"],
            "countryCode": "US",
        })
    return suppliers


def _build_purchase_orders(cfg: dict, rng: random.Random, suppliers: list[dict],
                           count: int, days: int) -> list[dict]:
    zips = sorted(cfg["zip_to_jurisdiction"])
    pos = []
    for i in range(1, count + 1):
        supplier = rng.choice(suppliers)
        order_date = ANCHOR_DATE - timedelta(days=rng.randint(1, days))
        lines = []
        for n in range(1, rng.randint(1, 3) + 1):
            code, desc = rng.choice(CODE_POOL_AP)
            unit_price = round(rng.uniform(25.0, 1800.0), 2)
            quantity = rng.randint(1, 5)
            lines.append({
                "lineId": str(n),
                "sku": f"SKU-{code}",
                "description": desc,
                "taxCode": code,
                "quantity": quantity,
                "unitPrice": unit_price,
                "amount": round(unit_price * quantity, 2),
            })
        pos.append({
            "poId": f"PO-{i:05d}",
            "supplierCode": supplier["supplierCode"],
            "orderDate": order_date.isoformat(),
            "deliveryZip": zips[(i * 5) % len(zips)],
            "currency": cfg.get("currency", "USD"),
            "status": "open",
            "lines": lines,
        })
    return pos


def _build_receipts(rng: random.Random, pos: list[dict]) -> list[dict]:
    receipts = []
    for i, po in enumerate(pos, start=1):
        received = ANCHOR_DATE - timedelta(days=rng.randint(1, 20))
        receipts.append({
            "receiptId": f"GR-{i:05d}",
            "poId": po["poId"],
            "receivedDate": received.isoformat(),
            "lines": [
                {"lineId": line["lineId"], "quantityReceived": line["quantity"]}
                for line in po["lines"]
            ],
        })
    return receipts


def _build_vendor_invoices(cfg: dict, rng: random.Random, suppliers: list[dict],
                           pos: list[dict], count: int, days: int) -> list[dict]:
    by_code = {s["supplierCode"]: s for s in suppliers}
    vouchers = []
    for i in range(1, count + 1):
        po = pos[(i * 7) % len(pos)]
        supplier = by_code[po["supplierCode"]]
        invoice_date = ANCHOR_DATE - timedelta(days=rng.randint(0, days))
        state = _state_for_zip(cfg, po["deliveryZip"])
        vendor_charges = rng.random() < 0.4
        lines = []
        subtotal = 0.0
        tax_charged_total = 0.0
        for po_line in po["lines"]:
            taxable = bool(cfg["tax_codes"][po_line["taxCode"]]["taxable_states"].get(state, False))
            tax_charged = (round(po_line["amount"] * VENDOR_TAX_RATE, 2)
                           if vendor_charges and taxable else 0.0)
            lines.append({
                "lineId": po_line["lineId"],
                "poLineId": po_line["lineId"],
                "sku": po_line["sku"],
                "description": po_line["description"],
                "taxCode": po_line["taxCode"],
                "quantity": po_line["quantity"],
                "unitPrice": po_line["unitPrice"],
                "amount": po_line["amount"],
                "taxCharged": tax_charged,
            })
            subtotal = round(subtotal + po_line["amount"], 2)
            tax_charged_total = round(tax_charged_total + tax_charged, 2)
        vouchers.append({
            "voucherId": f"VINV-{i:05d}",
            "supplierInvoiceNumber": f"{supplier['supplierCode'][-3:]}-{i:04d}",
            "supplierCode": supplier["supplierCode"],
            "poId": po["poId"],
            "invoiceDate": invoice_date.isoformat(),
            "dueDate": (invoice_date + timedelta(
                days=int(cfg.get("ap", {}).get("payment_terms_days", 30)))).isoformat(),
            "deliveryZip": po["deliveryZip"],
            "supplierZip": supplier["supplierZip"],
            "currency": cfg.get("currency", "USD"),
            "subtotal": subtotal,
            "taxChargedTotal": tax_charged_total,
            "lines": lines,
            "tax": None,
            "match": None,
            "status": "received",
            "source": "json",
        })
    return vouchers


def _apply_injections(vouchers: list[dict], inject: list[str]) -> list[dict]:
    injected = []
    if "price_variance" in inject and len(vouchers) > 5:
        target = vouchers[5]
        target["lines"][0]["unitPrice"] = round(target["lines"][0]["unitPrice"] + 12.50, 2)
        target["lines"][0]["amount"] = round(
            target["lines"][0]["unitPrice"] * target["lines"][0]["quantity"], 2)
        target["subtotal"] = round(sum(line["amount"] for line in target["lines"]), 2)
        injected.append({"break": "price_variance", "voucherId": target["voucherId"],
                         "effect": "invoice priced above the PO -> PRICE_VARIANCE -> blocked"})
    if "no_po" in inject and len(vouchers) > 7:
        target = vouchers[7]
        target["poId"] = None
        injected.append({"break": "no_po", "voucherId": target["voucherId"],
                         "effect": "invoice with no purchase order -> NO_PO -> blocked"})
    if "duplicate_invoice" in inject and len(vouchers) > 9:
        source, target = vouchers[3], vouchers[9]
        target["supplierCode"] = source["supplierCode"]
        target["supplierInvoiceNumber"] = source["supplierInvoiceNumber"]
        injected.append({"break": "duplicate_invoice", "voucherId": target["voucherId"],
                         "effect": f"duplicate of {source['voucherId']} -> DUPLICATE -> blocked"})
    return injected


def _fresh_ap_workspace(paths: Paths) -> None:
    for d in (paths.procurement, paths.ap, paths.einvoicing):
        robust_rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    for d in (paths.ap_tieouts, paths.parallel_run, paths.go_live):
        robust_rmtree(d)
        d.mkdir(parents=True, exist_ok=True)
    tax_mart.init_schema(paths)
    tax_mart.reset_ap(paths)


def run_seed_ap(cfg: dict | None = None, root=None, inject: list[str] | None = None,
                reset: bool = False) -> dict:
    cfg = cfg if cfg is not None else load_config(root)
    paths = get_paths(cfg, root)
    paths.ensure()
    inject = list(inject or [])
    seed_cfg = cfg["seed_ap"]
    rng = random.Random(seed_cfg["rng_seed"])

    # An AP-only run shares the engine transaction log and the GL journal with
    # the AR domain, so a stale run would corrupt the tie-outs. Reset the whole
    # workspace first unless an AR seed has already done so ('-Phase all').
    if reset:
        _fresh_workspace(paths)
    _fresh_ap_workspace(paths)

    suppliers = _build_suppliers(cfg, rng, seed_cfg["suppliers"])
    pos = _build_purchase_orders(cfg, rng, suppliers, seed_cfg["purchase_orders"],
                                 seed_cfg["history_days"])
    receipts = _build_receipts(rng, pos)
    vouchers = _build_vendor_invoices(cfg, rng, suppliers, pos,
                                      seed_cfg["vendor_invoices"], seed_cfg["history_days"])
    injected = _apply_injections(vouchers, inject)

    procurement.save_suppliers(paths, suppliers)
    procurement.save_purchase_orders(paths, pos)
    procurement.save_receipts(paths, receipts)

    xml_count = int(len(vouchers) * float(seed_cfg["xml_share"]))
    json_vouchers = vouchers[xml_count:]
    xml_vouchers = vouchers[:xml_count]

    inbox = paths.einvoicing / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for voucher in xml_vouchers:
        atomic_write_einvoice(inbox / f"{voucher['voucherId']}.xml", voucher)

    ap.save_vouchers(paths, json_vouchers)
    ingest = ap.ingest_einvoice_inbox(paths)

    events = [
        {
            "eventId": f"EVT-AP-{i + 1:05d}",
            "type": "vendor_invoice.received",
            "voucherId": v["voucherId"],
            "createdAt": f"{v['invoiceDate']}T08:00:00+00:00",
            "status": "pending",
        }
        for i, v in enumerate(vouchers)
    ]
    ap.save_outbox(paths, events)
    atomic_write_json(paths.ap / "idempotency.json", {})

    tax_mart.upsert_suppliers(paths, suppliers)

    return {
        "anchorDate": ANCHOR_DATE.isoformat(),
        "workspaceRoot": str(paths.root),
        "suppliers": len(suppliers),
        "purchaseOrders": len(pos),
        "goodsReceipts": len(receipts),
        "vendorInvoices": len(vouchers),
        "xmlInvoices": ingest["files"],
        "xmlParsed": len(ingest["parsed"]),
        "xmlParseErrors": ingest["errors"],
        "injected": injected,
        "rngSeed": seed_cfg["rng_seed"],
    }


def atomic_write_einvoice(path, voucher) -> None:
    from ..util import atomic_write_text
    atomic_write_text(path, einvoicing.to_einvoice(voucher))
