"""Shared helpers: money rounding, hashing, ids, timestamps."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

CENT = Decimal("0.01")


def dec(value) -> Decimal:
    return Decimal(str(value))


def cents(value) -> Decimal:
    """Round half-up to the nearest cent (the way tax engines do)."""
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def d2f(value: Decimal) -> float:
    return float(cents(value))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def sha256_hex(payload) -> str:
    if not isinstance(payload, (bytes, str)):
        payload = json.dumps(payload, sort_keys=True, default=str)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def short_hash(payload, length: int = 16) -> str:
    return sha256_hex(payload)[:length]


def id_hash(*parts, length: int = 16) -> str:
    return short_hash("|".join(str(p) for p in parts), length)


def new_run_id(job: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{job}-{stamp}"


def atomic_write_text(path: Path, text: str, retries: int = 5, delay_s: float = 0.05) -> None:
    """Write via temp file + os.replace, tolerating transient Windows/OneDrive locks.

    Cloud-sync clients (OneDrive) and indexers can briefly hold handles on the
    destination, making os.replace fail with PermissionError. Retry a few
    times, then fall back to an in-place write so the pipeline never dies on
    a sync hiccup.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == retries - 1:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                try:
                    tmp.unlink()
                except OSError:
                    pass
                return
            time.sleep(delay_s)


def atomic_write_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, default=str))


def robust_unlink(path: Path, retries: int = 10, delay_s: float = 0.2) -> None:
    """Delete a file, retrying while cloud-sync/indexers hold transient locks."""
    path = Path(path)
    for attempt in range(retries):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay_s)


def robust_rmtree(path: Path, retries: int = 10, delay_s: float = 0.2) -> None:
    """Remove a directory tree, retrying around transient Windows locks."""
    path = Path(path)
    if not path.exists():
        return
    for attempt in range(retries):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except (PermissionError, OSError):
            if attempt == retries - 1:
                raise
            time.sleep(delay_s)
