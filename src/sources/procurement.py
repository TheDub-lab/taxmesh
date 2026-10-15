"""Fincore procurement synthetic source: supplier master, POs, goods receipts."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Paths
from ..util import atomic_write_json


def _path(paths: Paths, name: str) -> Path:
    return paths.procurement / name


def load_suppliers(paths: Paths) -> list[dict]:
    p = _path(paths, "suppliers.json")
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_suppliers(paths: Paths, suppliers: list[dict]) -> None:
    atomic_write_json(_path(paths, "suppliers.json"), suppliers)


def find_supplier(paths: Paths, supplier_code: str) -> dict | None:
    for sup in load_suppliers(paths):
        if sup["supplierCode"] == supplier_code:
            return sup
    return None


def load_purchase_orders(paths: Paths) -> list[dict]:
    p = _path(paths, "purchase_orders.json")
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_purchase_orders(paths: Paths, pos: list[dict]) -> None:
    atomic_write_json(_path(paths, "purchase_orders.json"), pos)


def find_po(paths: Paths, po_id: str) -> dict | None:
    for po in load_purchase_orders(paths):
        if po["poId"] == po_id:
            return po
    return None


def load_receipts(paths: Paths) -> list[dict]:
    p = _path(paths, "goods_receipts.json")
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def save_receipts(paths: Paths, receipts: list[dict]) -> None:
    atomic_write_json(_path(paths, "goods_receipts.json"), receipts)


def receipts_for_po(paths: Paths, po_id: str) -> list[dict]:
    return [r for r in load_receipts(paths) if r["poId"] == po_id]


def received_quantity(paths: Paths, po_id: str, po_line_id: str) -> float:
    total = 0.0
    for receipt in receipts_for_po(paths, po_id):
        for line in receipt["lines"]:
            if line["lineId"] == po_line_id:
                total += float(line["quantityReceived"])
    return total
