"""Configuration and path resolution for taxmesh."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ONEDRIVE_ENV_VARS = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


def _synced_roots() -> list[Path]:
    roots: list[Path] = []
    for var in ONEDRIVE_ENV_VARS:
        value = os.environ.get(var)
        if value:
            try:
                roots.append(Path(value).resolve())
            except OSError:
                continue
    return roots


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def is_synced_folder(path: Path | str) -> bool:
    """True when a path lives under a cloud-sync root (e.g. OneDrive).

    Cloud clients manage folders under their root with reparse points and
    transient locks; deleting or rewriting files there can fail with WinError 5.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    if any(_is_under(resolved, root) for root in _synced_roots()):
        return True
    return any("onedrive" in part.lower() for part in resolved.parts)


def local_workspace_root() -> Path:
    """A scratch location outside cloud-synced folders."""
    env = os.environ.get("TAXMESH_ROOT")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "taxmesh"
    return REPO_ROOT / "data" / "workspace"


def resolve_workspace_root(root: Path | str | None = None) -> Path:
    """Where runtime data (data/, outputs/, landing/) is written.

    Precedence: an explicit --root argument, then $TAXMESH_ROOT, then the repo
    root — except that a repo living inside a synced folder is redirected to a
    local scratch directory, because cloud sync locks break atomic writes and
    directory removal mid-run.
    """
    if root is not None and str(root) not in ("", "."):
        return Path(root).expanduser()
    env = os.environ.get("TAXMESH_ROOT")
    if env:
        return Path(env).expanduser()
    if is_synced_folder(REPO_ROOT):
        return local_workspace_root()
    return REPO_ROOT


def load_config(root: Path | str | None = None) -> dict:
    base = Path(root) if root is not None else REPO_ROOT
    with open(base / "config.json", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass(frozen=True)
class Paths:
    root: Path
    data: Path
    outputs: Path
    landing: Path
    billing: Path
    erp: Path
    crm: Path
    engine: Path
    bus: Path
    procurement: Path
    ap: Path
    einvoicing: Path
    runs: Path
    tieouts: Path
    dlq: Path
    flow: Path
    ap_tieouts: Path
    parallel_run: Path
    go_live: Path
    epm: Path
    monitoring: Path

    @classmethod
    def from_config(cls, cfg: dict, root: Path | str | None = None) -> "Paths":
        base = resolve_workspace_root(root)
        p = cfg.get("paths", {})
        data = base / p.get("data", "data")
        outputs = base / p.get("outputs", "outputs")
        return cls(
            root=base,
            data=data,
            outputs=outputs,
            landing=base / p.get("landing", "landing"),
            billing=data / "billing",
            erp=data / "erp",
            crm=data / "crm",
            engine=data / "engine",
            bus=data / "bus",
            procurement=data / "procurement",
            ap=data / "ap",
            einvoicing=data / "einvoicing",
            runs=outputs / "runs",
            tieouts=outputs / "tieouts",
            dlq=outputs / "dlq",
            flow=outputs / "flow",
            ap_tieouts=outputs / "ap_tieouts",
            parallel_run=outputs / "parallel_run",
            go_live=outputs / "go_live",
            epm=base / p.get("landing", "landing") / "epm",
            monitoring=outputs / "monitoring",
        )

    def ensure(self) -> None:
        for d in (
            self.data, self.outputs, self.landing, self.billing, self.erp,
            self.crm, self.engine, self.bus, self.procurement, self.ap,
            self.einvoicing, self.runs, self.tieouts, self.dlq, self.flow,
            self.ap_tieouts, self.parallel_run, self.go_live, self.epm,
            self.monitoring,
        ):
            d.mkdir(parents=True, exist_ok=True)


def get_paths(cfg: dict, root: Path | str | None = None) -> Paths:
    return Paths.from_config(cfg, root)
