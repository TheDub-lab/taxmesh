"""Shared fixtures: an isolated workspace with a copied config + in-process engine."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.config import REPO_ROOT, get_paths, load_config
from src.engine.app import create_app
from src.integrations.engine_client import EngineClient


@pytest.fixture
def workspace(tmp_path):
    shutil.copy(REPO_ROOT / "config.json", tmp_path / "config.json")
    cfg = load_config(tmp_path)
    cfg["seed"]["invoices"] = 8  # small but representative
    paths = get_paths(cfg, tmp_path)
    paths.ensure()
    return SimpleNamespace(root=tmp_path, cfg=cfg, paths=paths)


@pytest.fixture
def engine(workspace):
    app = create_app(cfg=workspace.cfg, root=workspace.root)
    return EngineClient(client=TestClient(app))


@pytest.fixture
def ap_workspace(tmp_path):
    shutil.copy(REPO_ROOT / "config.json", tmp_path / "config.json")
    cfg = load_config(tmp_path)
    cfg["seed_ap"].update({"vendor_invoices": 12, "purchase_orders": 10})
    paths = get_paths(cfg, tmp_path)
    paths.ensure()
    return SimpleNamespace(root=tmp_path, cfg=cfg, paths=paths)


@pytest.fixture
def ap_engine(ap_workspace):
    app = create_app(cfg=ap_workspace.cfg, root=ap_workspace.root)
    return EngineClient(client=TestClient(app))
