"""E-invoicing: a reduced, UBL-shaped synthetic invoice XML.

This is NOT certified UBL and makes no compliance claim. It is a deliberately
small subset that mirrors the *shape* of a UBL Invoice document closely enough
to exercise a real integration:

    Invoice / ID / IssueDate / AccountingSupplierParty / PurchaseOrderReference /
    InvoiceLine / InvoicedQuantity / LineExtensionAmount / Price / Item / TaxCharged

The point of the exercise is the round trip: render a vendor invoice to XML,
parse it back, and prove the parse returns the same facts, with parse coverage
reported as a go-live gate.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

NS = "urn:taxmesh:synthetic:invoice:1"
Q = f"{{{NS}}}"


def _text(parent, tag: str, value) -> ET.Element:
    el = ET.SubElement(parent, Q + tag)
    el.text = "" if value is None else str(value)
    return el


def _find(parent, tag: str):
    return parent.find(Q + tag)


def to_einvoice(voucher: dict) -> str:
    """Render a vendor-invoice record to reduced UBL-shaped XML."""
    invoice = ET.Element(Q + "Invoice")
    _text(invoice, "ID", voucher["voucherId"])
    _text(invoice, "IssueDate", voucher["invoiceDate"])
    _text(invoice, "SupplierInvoiceNumber", voucher["supplierInvoiceNumber"])
    _text(invoice, "DocumentCurrencyCode", voucher.get("currency", "USD"))
    _text(invoice, "DeliveryZip", voucher["deliveryZip"])
    if voucher.get("poId"):
        _text(invoice, "PurchaseOrderReference", voucher["poId"])

    supplier = ET.SubElement(invoice, Q + "AccountingSupplierParty")
    party = ET.SubElement(supplier, Q + "PartyIdentification")
    _text(party, "ID", voucher["supplierCode"])

    for line in voucher["lines"]:
        line_el = ET.SubElement(invoice, Q + "InvoiceLine")
        _text(line_el, "ID", line["lineId"])
        qty = _text(line_el, "InvoicedQuantity", _fmt(line["quantity"]))
        qty.set("unitCode", "EA")
        amount = _text(line_el, "LineExtensionAmount", _fmt(line["amount"]))
        amount.set("currencyID", voucher.get("currency", "USD"))
        _text(line_el, "TaxCharged", _fmt(line.get("taxCharged", 0.0)))
        price = ET.SubElement(line_el, Q + "Price")
        price_amount = _text(price, "PriceAmount", _fmt(line["unitPrice"]))
        price_amount.set("currencyID", voucher.get("currency", "USD"))
        item = ET.SubElement(line_el, Q + "Item")
        _text(item, "Name", line.get("description", ""))
        category = ET.SubElement(item, Q + "ClassifiedTaxCategory")
        _text(category, "ID", line["taxCode"])

    ET.indent(invoice, space="  ")
    body = ET.tostring(invoice, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'


def parse_einvoice(xml_text: str) -> dict:
    """Parse reduced UBL-shaped XML back into a vendor-invoice record."""
    root = ET.fromstring(xml_text)
    if root.tag != Q + "Invoice":
        raise ValueError(f"unexpected root element {root.tag!r}")

    def txt(tag: str, default=None):
        el = _find(root, tag)
        return el.text if el is not None else default

    supplier_el = _find(root, "AccountingSupplierParty")
    supplier_id = None
    if supplier_el is not None:
        party = supplier_el.find(Q + "PartyIdentification")
        if party is not None:
            id_el = party.find(Q + "ID")
            supplier_id = id_el.text if id_el is not None else None
    if not supplier_id:
        raise ValueError("missing supplier party identification")

    lines = []
    subtotal = 0.0
    tax_charged_total = 0.0
    for line_el in root.findall(Q + "InvoiceLine"):
        amount = float(_find(line_el, "LineExtensionAmount").text)
        tax_charged = float(_find(line_el, "TaxCharged").text or 0.0)
        price_el = _find(line_el, "Price")
        price_amount = price_el.find(Q + "PriceAmount").text
        item = _find(line_el, "Item")
        name_el = item.find(Q + "Name") if item is not None else None
        category = item.find(Q + "ClassifiedTaxCategory") if item is not None else None
        tax_code_el = category.find(Q + "ID") if category is not None else None
        if tax_code_el is None or not tax_code_el.text:
            raise ValueError("invoice line is missing a tax category")
        lines.append({
            "lineId": _find(line_el, "ID").text,
            "description": name_el.text if name_el is not None else "",
            "taxCode": tax_code_el.text,
            "quantity": _num(_find(line_el, "InvoicedQuantity").text),
            "unitPrice": float(price_amount),
            "amount": amount,
            "taxCharged": tax_charged,
        })
        subtotal = round(subtotal + amount, 2)
        tax_charged_total = round(tax_charged_total + tax_charged, 2)

    if not lines:
        raise ValueError("invoice has no lines")

    return {
        "voucherId": txt("ID"),
        "supplierInvoiceNumber": txt("SupplierInvoiceNumber"),
        "supplierCode": supplier_id,
        "invoiceDate": txt("IssueDate"),
        "dueDate": None,
        "deliveryZip": txt("DeliveryZip"),
        "poId": txt("PurchaseOrderReference"),
        "currency": txt("DocumentCurrencyCode", "USD"),
        "subtotal": subtotal,
        "taxChargedTotal": tax_charged_total,
        "lines": lines,
        "tax": None,
        "match": None,
        "status": "received",
        "source": "xml",
    }


def _fmt(value) -> str:
    return f"{float(value):.2f}"


def _num(value: str) -> float:
    return float(value) if "." in value else int(value)
