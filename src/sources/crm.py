"""Relate CRM (Salesforce-flavored) synthetic source: accounts + certificates."""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Paths
from ..util import atomic_write_json


def _read(paths: Paths, name: str) -> list[dict]:
    p: Path = paths.crm / name
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def load_accounts(paths: Paths) -> list[dict]:
    return _read(paths, "accounts.json")


def load_certificates(paths: Paths) -> list[dict]:
    return _read(paths, "certificates.json")


def write_certificates(paths: Paths, certs: list[dict]) -> None:
    atomic_write_json(paths.crm / "certificates.json", certs)
