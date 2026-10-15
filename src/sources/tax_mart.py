"""Tax mart — the SQLite data warehouse target for tax facts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..config import Paths
from ..util import now_iso, robust_unlink

SCHEMA = """
CREATE TABLE IF NOT EXISTS dim_customers (
    customer_code TEXT PRIMARY KEY,
    name TEXT,
    ship_to_zip TEXT,
    state_code TEXT,
    withholding_applicable INTEGER
);
CREATE TABLE IF NOT EXISTS dim_jurisdictions (
    jurisdiction_id TEXT PRIMARY KEY,
    state_code TEXT,
    state_name TEXT,
    county_name TEXT,
    city_name TEXT,
    total_rate REAL
);
CREATE TABLE IF NOT EXISTS fct_invoices (
    invoice_id TEXT PRIMARY KEY,
    customer_code TEXT,
    issue_date TEXT,
    status TEXT,
    jurisdiction_id TEXT,
    subtotal REAL,
    tax_total REAL,
    withholding_total REAL,
    engine_txn_id TEXT,
    loaded_at TEXT
);
CREATE TABLE IF NOT EXISTS fct_tax_lines (
    invoice_id TEXT,
    line_id TEXT,
    jurisdiction_id TEXT,
    jurisdiction_type TEXT,
    rate REAL,
    tax_amount REAL,
    PRIMARY KEY (invoice_id, line_id, jurisdiction_id, jurisdiction_type)
);
CREATE TABLE IF NOT EXISTS fct_gl_postings (
    entry_id TEXT,
    row_seq INTEGER,
    invoice_id TEXT,
    account TEXT,
    debit REAL,
    credit REAL,
    jurisdiction_id TEXT,
    PRIMARY KEY (entry_id, row_seq)
);
"""


def db_path(paths: Paths) -> Path:
    return paths.data / "tax_mart.sqlite"


def connect(paths: Paths) -> sqlite3.Connection:
    con = sqlite3.connect(db_path(paths))
    con.row_factory = sqlite3.Row
    return con


class _Session:
    """Explicit open/commit/close — `with sqlite3.connect(...)` does not close."""

    def __init__(self, paths: Paths):
        self.con = connect(paths)

    def __enter__(self) -> sqlite3.Connection:
        return self.con

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.con.commit()
        finally:
            self.con.close()
        return False


def init_schema(paths: Paths) -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    with _Session(paths) as con:
        con.executescript(SCHEMA)


def reset(paths: Paths) -> None:
    robust_unlink(db_path(paths))
    init_schema(paths)


def upsert_customers(paths: Paths, customers: list[dict]) -> None:
    with _Session(paths) as con:
        con.executemany(
            "INSERT OR REPLACE INTO dim_customers VALUES (?,?,?,?,?)",
            [
                (
                    c["customerCode"], c["name"], c["shipToZip"],
                    c.get("stateCode", ""), 1 if c.get("withholdingApplicable") else 0,
                )
                for c in customers
            ],
        )


def upsert_jurisdictions(paths: Paths, cfg: dict) -> None:
    rows = []
    for jid, j in cfg["jurisdictions"].items():
        total = round(sum(float(r) for r in j["rates"].values()), 6)
        rows.append((jid, j["state_code"], j["state_name"], j["county_name"],
                     j["city_name"], total))
    with _Session(paths) as con:
        con.executemany("INSERT OR REPLACE INTO dim_jurisdictions VALUES (?,?,?,?,?,?)", rows)


def load_invoice(paths: Paths, invoice: dict, tax_result: dict, gl_entry: dict) -> None:
    """Idempotent (delete + insert) load of one invoice across all fact tables."""
    inv_id = invoice["invoiceId"]
    with _Session(paths) as con:
        con.execute("DELETE FROM fct_invoices WHERE invoice_id = ?", (inv_id,))
        con.execute("DELETE FROM fct_tax_lines WHERE invoice_id = ?", (inv_id,))
        con.execute("DELETE FROM fct_gl_postings WHERE invoice_id = ?", (inv_id,))

        con.execute(
            "INSERT INTO fct_invoices VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                inv_id, invoice["customerCode"], invoice["issueDate"], invoice["status"],
                tax_result.get("jurisdictionId"), float(tax_result["subtotal"]),
                float(tax_result["totalTax"]),
                float(tax_result["withholding"]["amount"]),
                tax_result.get("engineTxnId"), now_iso(),
            ),
        )
        line_rows = []
        for line in tax_result["lines"]:
            for row in line["jurisdictions"]:
                line_rows.append(
                    (inv_id, line["lineId"], row["jurisdictionId"],
                     row["jurisdictionType"], row["rate"], row["tax"])
                )
        con.executemany("INSERT INTO fct_tax_lines VALUES (?,?,?,?,?,?)", line_rows)

        gl_rows = [
            (gl_entry["entryId"], i, gl_entry["invoiceId"], r["account"],
             r["debit"], r["credit"], r["jurisdictionId"])
            for i, r in enumerate(gl_entry["lines"])
        ]
        con.executemany("INSERT INTO fct_gl_postings VALUES (?,?,?,?,?,?,?)", gl_rows)


def invoice_rows(paths: Paths) -> list[dict]:
    with _Session(paths) as con:
        rows = con.execute("SELECT * FROM fct_invoices ORDER BY invoice_id").fetchall()
        return [dict(r) for r in rows]


def tax_line_totals_by_jurisdiction(paths: Paths) -> dict[str, float]:
    with _Session(paths) as con:
        rows = con.execute(
            "SELECT jurisdiction_id, ROUND(SUM(tax_amount), 2) AS tax "
            "FROM fct_tax_lines GROUP BY jurisdiction_id"
        ).fetchall()
        return {r["jurisdiction_id"]: float(r["tax"]) for r in rows}
