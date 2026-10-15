"""E-invoicing reduced-UBL render/parse round trip."""

from __future__ import annotations

import pytest

from src.sources import einvoicing

VOUCHER = {
    "voucherId": "VINV-00042",
    "supplierInvoiceNumber": "SUP-042-0007",
    "supplierCode": "SUP-042",
    "invoiceDate": "2026-09-12",
    "deliveryZip": "94105",
    "poId": "PO-00007",
    "currency": "USD",
    "lines": [
        {"lineId": "1", "description": "Wide format printer", "taxCode": "HARDWARE",
         "quantity": 3, "unitPrice": 500.0, "amount": 1500.0, "taxCharged": 108.75},
        {"lineId": "2", "description": "Inbound freight", "taxCode": "FREIGHT-IN",
         "quantity": 1, "unitPrice": 75.0, "amount": 75.0, "taxCharged": 0.0},
    ],
    "subtotal": 1575.0,
    "taxChargedTotal": 108.75,
}


def test_round_trip_preserves_facts():
    parsed = einvoicing.parse_einvoice(einvoicing.to_einvoice(VOUCHER))

    assert parsed["voucherId"] == VOUCHER["voucherId"]
    assert parsed["supplierInvoiceNumber"] == VOUCHER["supplierInvoiceNumber"]
    assert parsed["supplierCode"] == VOUCHER["supplierCode"]
    assert parsed["invoiceDate"] == VOUCHER["invoiceDate"]
    assert parsed["deliveryZip"] == VOUCHER["deliveryZip"]
    assert parsed["poId"] == VOUCHER["poId"]
    assert parsed["source"] == "xml"

    assert [l["lineId"] for l in parsed["lines"]] == ["1", "2"]
    assert parsed["lines"][0]["taxCode"] == "HARDWARE"
    assert parsed["lines"][0]["quantity"] == 3
    assert parsed["lines"][0]["unitPrice"] == 500.0
    assert parsed["lines"][0]["taxCharged"] == 108.75
    assert parsed["lines"][1]["taxCharged"] == 0.0

    assert parsed["subtotal"] == 1575.0
    assert parsed["taxChargedTotal"] == 108.75


def test_render_is_stable_under_round_trip():
    once = einvoicing.to_einvoice(VOUCHER)
    twice = einvoicing.to_einvoice(einvoicing.parse_einvoice(once))
    assert once == twice


def test_voucher_without_po_omits_reference():
    parsed = einvoicing.parse_einvoice(einvoicing.to_einvoice({**VOUCHER, "poId": None}))
    assert parsed["poId"] is None


def test_parse_rejects_wrong_root():
    with pytest.raises(ValueError):
        einvoicing.parse_einvoice("<NotAnInvoice/>")


def test_parse_requires_line_tax_category():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <Invoice xmlns="urn:taxmesh:synthetic:invoice:1">
      <ID>VINV-1</ID><IssueDate>2026-09-12</IssueDate>
      <SupplierInvoiceNumber>X-1</SupplierInvoiceNumber><DeliveryZip>94105</DeliveryZip>
      <AccountingSupplierParty><PartyIdentification><ID>SUP-1</ID></PartyIdentification>
      </AccountingSupplierParty>
      <InvoiceLine>
        <ID>1</ID><InvoicedQuantity unitCode="EA">1</InvoicedQuantity>
        <LineExtensionAmount currencyID="USD">10.00</LineExtensionAmount>
        <TaxCharged>0.00</TaxCharged>
        <Price><PriceAmount currencyID="USD">10.00</PriceAmount></Price>
        <Item><Name>Thing</Name></Item>
      </InvoiceLine>
    </Invoice>"""
    with pytest.raises(ValueError):
        einvoicing.parse_einvoice(xml)
